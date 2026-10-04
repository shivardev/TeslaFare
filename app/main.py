from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.chargers.supercharge_info import SuperchargeInfoProvider
from app.config import vehicle as vehicle_cfg
from app.config.settings import settings
from app.db.cache import CacheDB
from app.geocoding.photon import PhotonGeocoder
from app.models import Charger, Coordinate, PricingSchedule, TripResponse
from app.optimizer.graph import build_graph
from app.optimizer.search import OptimizerConfig, choose_useful_plans, optimize_departure
from app.pricing.tesla import TeslaPriceProvider
from app.routing.osrm import OSRMRouteProvider
from app.timezones import TimeApiTimezoneProvider
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("tesla-cheap-trip")

BASE_DIR = Path(__file__).resolve().parent
cache = CacheDB(settings.cache_db_path)
geocoder = PhotonGeocoder(settings.photon_base_url, cache, settings.request_timeout_seconds, settings.user_agent)
router = OSRMRouteProvider(settings.osrm_base_url, cache, settings.request_timeout_seconds, settings.user_agent)
chargers_provider = SuperchargeInfoProvider(
    settings.supercharge_info_url,
    cache,
    settings.request_timeout_seconds,
    settings.user_agent,
    settings.tesla_findus_base_url,
)
timezone_provider = TimeApiTimezoneProvider(
    settings.timezone_api_url,
    cache,
    settings.request_timeout_seconds,
    settings.user_agent,
    settings.timezone_cache_days,
)
price_provider = TeslaPriceProvider(
    cache,
    settings.pricing_request_timeout_seconds,
    settings.user_agent,
    settings.pricing_cache_hours,
    settings.tesla_playwright_fallback,
    settings.tesla_playwright_max_fallbacks,
)
energy = EnergyModel(vehicle_cfg.BATTERY_USABLE_KWH, vehicle_cfg.HIGHWAY_WH_PER_MILE)
charging = ChargingModel(vehicle_cfg.BATTERY_USABLE_KWH, vehicle_cfg.CHARGING_CURVE_KW)
optimizer_cfg = OptimizerConfig(
    starting_soc=vehicle_cfg.STARTING_SOC,
    min_charger_soc=vehicle_cfg.MIN_CHARGER_SOC,
    destination_soc=vehicle_cfg.DESTINATION_SOC,
    max_preferred_charge_soc=vehicle_cfg.MAX_PREFERRED_CHARGE_SOC,
    absolute_max_charge_soc=vehicle_cfg.ABSOLUTE_MAX_CHARGE_SOC,
    max_route_detour_percent=settings.max_route_detour_percent,
    max_total_extra_driving_minutes=settings.max_total_extra_driving_minutes,
    results_per_departure=settings.max_results_per_departure,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await price_provider.close()


app = FastAPI(title="Tesla Cheap Trip", version="0.1.2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class TripRequest(BaseModel):
    from_location: str = Field(min_length=2, max_length=200)
    to_location: str = Field(min_length=2, max_length=200)


def _ceil_departure(now: datetime, interval_minutes: int) -> datetime:
    discard = timedelta(minutes=now.minute % interval_minutes, seconds=now.second, microseconds=now.microsecond)
    rounded = now - discard
    if discard.total_seconds() > 0:
        rounded += timedelta(minutes=interval_minutes)
    return rounded


def _departure_candidates(origin_tz: str | None) -> list[datetime]:
    tz = ZoneInfo(origin_tz) if origin_tz else timezone.utc
    start = _ceil_departure(datetime.now(timezone.utc).astimezone(tz), settings.departure_interval_minutes)
    count = max(1, int(settings.departure_search_hours * 60 / settings.departure_interval_minutes))
    return [start + timedelta(minutes=i * settings.departure_interval_minutes) for i in range(count)]


async def _fill_timezones(candidates: list[Charger]) -> None:
    semaphore = asyncio.Semaphore(8)

    async def one(charger: Charger) -> None:
        async with semaphore:
            charger.timezone = await timezone_provider.timezone_at(
                charger.coordinate.lat, charger.coordinate.lon
            )

    await asyncio.gather(*(one(c) for c in candidates))


async def _fetch_prices(candidates: list[Charger]) -> dict[str, PricingSchedule]:
    semaphore = asyncio.Semaphore(8)

    async def one(charger: Charger):
        async with semaphore:
            return charger.location_id, await price_provider.get_prices(charger)

    tasks = [asyncio.create_task(one(c)) for c in candidates]
    pricing: dict[str, PricingSchedule] = {}
    completed = 0
    for task in asyncio.as_completed(tasks):
        try:
            station_id, schedule = await task
            pricing[station_id] = schedule
        except Exception as exc:
            log.warning("Pricing fetch task failed: %s", exc)
        completed += 1
        if completed == len(tasks) or completed % 5 == 0:
            known = sum(1 for p in pricing.values() if p.kind in {"flat", "time_of_use"})
            log.info("Pricing progress: %d/%d checked, %d usable", completed, len(tasks), known)
    return pricing


async def _plan_geometry(origin: Coordinate, destination: Coordinate, stops) -> list[list[float]]:
    points = [origin] + [s.coordinate for s in stops] + [destination]
    geometry: list[list[float]] = []
    for a, b in zip(points, points[1:]):
        routes = await router.route(a, b, alternatives=False)
        leg = routes[0].geometry
        if geometry and leg:
            geometry.extend(leg[1:])
        else:
            geometry.extend(leg)
    return geometry


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/debug/prices")
async def pricing_debug_api():
    return {"rows": cache.pricing_debug_rows()}


@app.get("/debug/prices", response_class=HTMLResponse)
async def pricing_debug_page(request: Request):
    return templates.TemplateResponse(request=request, name="prices.html", context={"rows": cache.pricing_debug_rows()})


@app.post("/api/trip", response_model=TripResponse)
async def trip(req: TripRequest) -> TripResponse:
    try:
        origin, destination = await asyncio.gather(
            geocoder.geocode(req.from_location), geocoder.geocode(req.to_location)
        )
        log.info("Origin resolved: %s %.5f,%.5f", origin.label, origin.coordinate.lat, origin.coordinate.lon)
        log.info("Destination resolved: %s %.5f,%.5f", destination.label, destination.coordinate.lat, destination.coordinate.lon)

        base_routes = await router.route(origin.coordinate, destination.coordinate, alternatives=True)
        base_route = min(base_routes, key=lambda r: r.duration_minutes)
        log.info("Base route: %.1f miles / %.0f minutes", base_route.distance_miles, base_route.duration_minutes)

        all_chargers = await chargers_provider.all_open()
        log.info("Open Superchargers loaded: %d; filtering route corridor...", len(all_chargers))
        candidates = chargers_provider.corridor_candidates(
            all_chargers,
            base_route.geometry,
            settings.corridor_miles,
            settings.max_candidate_chargers,
        )
        log.info("Candidate chargers: %d; fetching Tesla public pricing...", len(candidates))

        pricing = await _fetch_prices(candidates)
        priced = [
            c for c in candidates
            if pricing.get(c.location_id) and pricing[c.location_id].kind in {"flat", "time_of_use"}
        ]
        unknown = len(candidates) - len(priced)

        # Only time-of-use pricing needs an exact station timezone. Avoid dozens of
        # unnecessary timezone API calls for flat-price or unknown-price stations.
        tou_candidates = [c for c in priced if pricing[c.location_id].kind == "time_of_use"]
        if tou_candidates:
            log.info("Resolving local timezones for %d time-of-use charger(s)...", len(tou_candidates))
            await _fill_timezones(tou_candidates)

        timezone_missing = [
            c for c in priced
            if pricing[c.location_id].kind == "time_of_use" and not c.timezone
        ]
        usable = [
            c for c in priced
            if pricing[c.location_id].kind == "flat" or c.timezone
        ]
        log.info(
            "Pricing available: %d; unknown: %d; TOU timezone unavailable: %d",
            len(priced), unknown, len(timezone_missing),
        )

        warnings: list[str] = []
        if unknown:
            warnings.append(
                f"{unknown} candidate Supercharger(s) had PRICE UNKNOWN and were excluded from cost optimization. "
                "See /debug/prices for fetch details."
            )
        if timezone_missing:
            warnings.append(
                f"{len(timezone_missing)} time-of-use priced Supercharger(s) were excluded because their local "
                "timezone could not be resolved. The app will not guess UTC for local Tesla pricing windows."
            )
        if not usable and energy.reachable(vehicle_cfg.STARTING_SOC, base_route.distance_miles, vehicle_cfg.DESTINATION_SOC) is False:
            warnings.append("No priced Superchargers were available for a trip that requires charging, so no cost-optimal plan can be produced without inventing prices.")

        matrix_points = [origin.coordinate] + [c.coordinate for c in usable] + [destination.coordinate]
        distances, durations = await router.table(matrix_points)

        # Approximate a charger's off-route detour as half of the extra origin->charger->destination
        # time versus the fastest base route. This uses road time, not straight-line distance.
        keep_indices = [0]
        kept_usable: list[Charger] = []
        for i, charger in enumerate(usable, start=1):
            to_c = durations[0][i]
            from_c = durations[i][-1]
            if to_c is None or from_c is None:
                continue
            round_trip_detour = max(0.0, to_c + from_c - base_route.duration_minutes)
            one_way_detour_estimate = round_trip_detour / 2.0
            if one_way_detour_estimate <= settings.max_charger_detour_minutes + 1e-6:
                keep_indices.append(i)
                kept_usable.append(charger)
        keep_indices.append(len(matrix_points) - 1)
        if len(kept_usable) < len(usable):
            warnings.append(
                f"{len(usable) - len(kept_usable)} priced charger(s) were excluded by the "
                f"{settings.max_charger_detour_minutes}-minute charger-detour sanity limit."
            )
        distances = [[distances[i][j] for j in keep_indices] for i in keep_indices]
        durations = [[durations[i][j] for j in keep_indices] for i in keep_indices]

        graph = build_graph(
            origin.coordinate,
            destination.coordinate,
            kept_usable,
            pricing,
            distances,
            durations,
            base_route.distance_miles,
            base_route.duration_minutes,
        )

        origin_tz = await timezone_provider.timezone_at(
            origin.coordinate.lat, origin.coordinate.lon
        )
        if not origin_tz:
            warnings.append(
                "Origin timezone lookup failed, so departure timestamps are shown in UTC for this run. "
                "The next-24-hour search window is still evaluated; TOU chargers without local timezones remain excluded."
            )
        departures = _departure_candidates(origin_tz)
        all_plans = []
        states_evaluated = 0
        for depart in departures:
            plans, states = optimize_departure(graph, depart, energy, charging, optimizer_cfg)
            states_evaluated += states
            all_plans.extend(plans)
        log.info(
            "Optimization: departures tested=%d states evaluated=%d routes found=%d",
            len(departures), states_evaluated, len(all_plans),
        )

        chosen = choose_useful_plans(all_plans, base_route.distance_miles)
        for plan in chosen:
            plan.route_geometry = await _plan_geometry(origin.coordinate, destination.coordinate, plan.stops)
        if chosen:
            log.info("Best route: $%.2f / %.0f min / %d stops", chosen[0].charging_cost, chosen[0].total_minutes, len(chosen[0].stops))
        else:
            warnings.append("No feasible route was found within the configured SOC and detour constraints.")

        return TripResponse(
            origin=origin,
            destination=destination,
            base_route=base_route,
            candidate_chargers=len(candidates),
            pricing_available=len(priced),
            pricing_unknown=unknown,
            departures_tested=len(departures),
            plans=chosen,
            warnings=warnings,
            vehicle_assumptions={
                "battery_usable_kwh": vehicle_cfg.BATTERY_USABLE_KWH,
                "highway_wh_per_mile": vehicle_cfg.HIGHWAY_WH_PER_MILE,
                "starting_soc": vehicle_cfg.STARTING_SOC,
                "min_charger_soc": vehicle_cfg.MIN_CHARGER_SOC,
                "destination_soc": vehicle_cfg.DESTINATION_SOC,
                "max_preferred_charge_soc": vehicle_cfg.MAX_PREFERRED_CHARGE_SOC,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Trip calculation failed")
        raise HTTPException(status_code=502, detail=f"Trip calculation failed: {type(exc).__name__}: {exc}") from exc
