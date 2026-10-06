from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

from app.chargers.supercharge_info import SuperchargeInfoProvider
from app.chargers.knowledge_store import ChargerKnowledgeStore
from app.config import vehicle as vehicle_cfg
from app.config.settings import settings
from app.db.cache import CacheDB
from app.geocoding.census import CensusGeocoder
from app.geocoding.chain import FallbackGeocoder
from app.geocoding.photon import GeocodingNotFound, PhotonGeocoder
from app.models import CandidateCharger, Charger, Coordinate, PricingSchedule, RouteSummary, TripPlan, TripResponse
from app.optimizer.graph import GraphWaypoint, build_graph, graph_layout
from app.optimizer.explain import ChargerExplanation, explain_plan
from app.optimizer.graph import GraphContext
from app.optimizer.search import OptimizerConfig, choose_useful_plans, optimize_departure
from app.pricing.tesla import LiveLookupUnavailable, TeslaPriceProvider
from app.routing.osrm import OSRMRouteProvider
from app.timezones import TimeApiTimezoneProvider
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("teslafare")

BASE_DIR = Path(__file__).resolve().parent
cache = CacheDB(settings.cache_db_path)
charger_knowledge = ChargerKnowledgeStore(settings.charger_knowledge_path)
geocoder = FallbackGeocoder(
    place=PhotonGeocoder(settings.photon_base_url, cache, settings.request_timeout_seconds, settings.user_agent),
    address=CensusGeocoder(settings.census_geocoder_url, cache, settings.request_timeout_seconds, settings.user_agent),
)
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
    settings.tesla_playwright_headless,
    settings.tesla_playwright_browser,
    settings.tesla_browser_backend,
)
price_provider.requests_per_hour = settings.live_price_lookups_per_hour
_seeded = charger_knowledge.import_price_seed(settings.price_seed_path)
if _seeded:
    log.info("Loaded %d saved Supercharger prices from %s", _seeded, settings.price_seed_path)
optimizer_cfg = OptimizerConfig(
    starting_soc=vehicle_cfg.STARTING_SOC,
    min_charger_soc=vehicle_cfg.MIN_CHARGER_SOC,
    destination_soc=vehicle_cfg.DESTINATION_SOC,
    max_preferred_charge_soc=vehicle_cfg.MAX_PREFERRED_CHARGE_SOC,
    absolute_max_charge_soc=vehicle_cfg.ABSOLUTE_MAX_CHARGE_SOC,
    max_route_detour_percent=settings.max_route_detour_percent,
    max_total_extra_driving_minutes=settings.max_total_extra_driving_minutes,
    max_states=settings.optimizer_max_states,
    results_per_departure=settings.max_results_per_departure,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await price_provider.close()


app = FastAPI(title="TeslaFare", version="0.1.2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
trip_progress: dict[str, dict] = {}
# Recent trips' optimizer inputs, so the UI can ask "why not this charger?" without recomputing.
TRIP_CONTEXT_LIMIT = 20
trip_contexts: dict[str, tuple[GraphContext, OptimizerConfig, EnergyModel, ChargingModel]] = {}


def _remember_trip(trip_id: str, graph: GraphContext, cfg: OptimizerConfig, energy: EnergyModel, charging: ChargingModel) -> None:
    trip_contexts.pop(trip_id, None)
    trip_contexts[trip_id] = (graph, cfg, energy, charging)
    while len(trip_contexts) > TRIP_CONTEXT_LIMIT:
        trip_contexts.pop(next(iter(trip_contexts)))


def _trip_context(trip_id: str) -> tuple[GraphContext, OptimizerConfig, EnergyModel, ChargingModel]:
    context = trip_contexts.get(trip_id)
    if context is None:
        raise HTTPException(status_code=404, detail="This trip is no longer in memory. Plan it again to compare chargers.")
    return context

REPLAY_TRIPS = {
    "oct_2026_nashville_streetsboro": {
        "label": "Oct 2 Nashville to Streetsboro",
        "actual_cost": 38.35,
        "actual_kwh": 130.8516,
        "departure": "2026-10-02T00:30:00-05:00",
        "stations": [
            ("bowlinggreensupercharger", "Bowling Green, KY", 0.24),
            ("louisvillesupercharger", "Louisville, KY", 0.23),
            ("florencesupercharger", "Florence, KY - Houston Rd", 0.39),
            ("mtgileadsupercharger", "Mt. Gilead, OH", 0.49),
        ],
    }
}


def _publish_progress(progress_id: str | None, **updates) -> None:
    if not progress_id:
        return
    state = trip_progress.setdefault(progress_id, {"stage": "starting", "message": "Starting trip calculation", "chargers": []})
    state.update(updates)
    state["updated_at"] = time.time()
    # Keep this small for a long-running local process.
    if len(trip_progress) > 100:
        oldest = sorted(trip_progress, key=lambda key: trip_progress[key].get("updated_at", 0))[:20]
        for key in oldest:
            if key != progress_id:
                trip_progress.pop(key, None)


class StopRequest(BaseModel):
    location: str = Field(min_length=2, max_length=200)
    dwell_minutes: float = Field(default=0, ge=0, le=24 * 60)


class TripRequest(BaseModel):
    from_location: str = Field(min_length=2, max_length=200)
    to_location: str = Field(min_length=2, max_length=200)
    # Intermediate stops, visited strictly in the order given.
    stops: list[StopRequest] = Field(default_factory=list, max_length=8)
    fallback_price_per_kwh: float | None = Field(default=0.40, ge=0.01, le=2.0)
    excluded_station_ids: list[str] = Field(default_factory=list, max_length=100)
    use_charger_cache: bool = True
    progress_id: str | None = Field(default=None, min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    desired_departure_time: datetime | None = None
    departure_window_hours: int = Field(default=12, ge=2, le=24)
    replay_scenario: Literal["oct_2026_nashville_streetsboro"] | None = None
    starting_soc: float = Field(default=100.0, ge=1.0, le=100.0)
    min_charger_soc: float = Field(default=vehicle_cfg.MIN_CHARGER_SOC, ge=0.0, le=50.0)
    destination_soc: float = Field(default=vehicle_cfg.DESTINATION_SOC, ge=0.0, le=50.0)
    vehicle_profile_id: str = Field(default=vehicle_cfg.DEFAULT_VEHICLE_PROFILE_ID, max_length=80)
    custom_battery_usable_kwh: float | None = Field(default=None, ge=20.0, le=200.0)
    custom_highway_wh_per_mile: float | None = Field(default=None, ge=100.0, le=1000.0)
    custom_peak_charge_kw: float | None = Field(default=None, ge=20.0, le=400.0)

    @model_validator(mode="after")
    def validate_vehicle(self):
        vehicle_cfg.planning_profile(
            self.vehicle_profile_id,
            self.custom_battery_usable_kwh,
            self.custom_highway_wh_per_mile,
            self.custom_peak_charge_kw,
        )
        return self


class ExplainRequest(BaseModel):
    plan: TripPlan


class WhatIfRequest(BaseModel):
    plan: TripPlan
    station_id: str = Field(min_length=1, max_length=120)


class ManualPriceRequest(BaseModel):
    price_per_kwh: float = Field(ge=0.01, le=2.0)


def _leg_candidates(all_chargers: list[Charger], legs: list[RouteSummary]) -> list[Charger]:
    """Corridor chargers per leg. Progress is global (leg index + position on that leg), so the
    same station can appear once per leg it serves, e.g. on an out-and-back trip."""
    total_miles = sum(leg.distance_miles for leg in legs) or 1.0
    candidates: list[Charger] = []
    for leg_index, leg in enumerate(legs):
        cap = settings.max_candidate_chargers if len(legs) == 1 else max(
            8, round(settings.max_candidate_chargers * leg.distance_miles / total_miles)
        )
        for charger in chargers_provider.corridor_candidates(all_chargers, leg.geometry, settings.corridor_miles, cap):
            charger.route_leg = leg_index
            charger.route_progress = leg_index + charger.route_progress
            candidates.append(charger)
    return candidates


def _unique_stations(chargers: list[Charger]) -> list[Charger]:
    seen: dict[str, Charger] = {}
    for charger in chargers:
        current = seen.get(charger.location_id)
        if current is None or charger.corridor_distance_miles < current.corridor_distance_miles:
            seen[charger.location_id] = charger
    return sorted(seen.values(), key=lambda c: c.route_progress)


def _ceil_departure(now: datetime, interval_minutes: int) -> datetime:
    discard = timedelta(minutes=now.minute % interval_minutes, seconds=now.second, microseconds=now.microsecond)
    rounded = now - discard
    if discard.total_seconds() > 0:
        rounded += timedelta(minutes=interval_minutes)
    return rounded


def _departure_candidates(
    origin_tz: str | None,
    desired_departure: datetime | None = None,
    window_hours: int = 12,
) -> list[datetime]:
    tz = ZoneInfo(origin_tz) if origin_tz else timezone.utc
    if desired_departure is None:
        start = _ceil_departure(datetime.now(timezone.utc).astimezone(tz), settings.departure_interval_minutes)
        hours = settings.departure_search_hours
    else:
        anchor = desired_departure.replace(tzinfo=tz) if desired_departure.tzinfo is None else desired_departure.astimezone(tz)
        start = _ceil_departure(anchor - timedelta(hours=window_hours / 2), settings.departure_interval_minutes)
        hours = window_hours
    count = max(1, int(hours * 60 / settings.departure_interval_minutes) + 1)
    return [start + timedelta(minutes=i * settings.departure_interval_minutes) for i in range(count)]


async def _fill_timezones(candidates: list[Charger]) -> None:
    semaphore = asyncio.Semaphore(8)

    async def one(charger: Charger) -> None:
        async with semaphore:
            charger.timezone = await timezone_provider.timezone_at(
                charger.coordinate.lat, charger.coordinate.lon
            )

    await asyncio.gather(*(one(c) for c in candidates))


USABLE_PRICE_KINDS = {"flat", "time_of_use"}


# progress_id -> event set when the user asks to stop waiting for live prices.
pricing_skip_events: dict[str, asyncio.Event] = {}


def _recent_failure(schedule: PricingSchedule | None) -> bool:
    if schedule is None or schedule.kind in USABLE_PRICE_KINDS or schedule.fetched_at is None:
        return False
    fetched = schedule.fetched_at if schedule.fetched_at.tzinfo else schedule.fetched_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < timedelta(minutes=settings.pricing_retry_failed_minutes)


async def _fetch_prices(
    candidates: list[Charger],
    use_charger_cache: bool = True,
    progress_id: str | None = None,
) -> dict[str, PricingSchedule]:
    """Prices for each candidate. Stations still pending when the deadline passes (or when the user
    skips) are left out, so the caller falls back to the planning estimate for them."""
    price_provider.reset_browser_budget()
    semaphore = asyncio.Semaphore(8)

    async def one(charger: Charger):
        async with semaphore:
            saved = charger_knowledge.pricing(charger.location_id)
            saved_usable = saved is not None and saved.kind in USABLE_PRICE_KINDS
            # Real prices are reused; a failure is retried, but not again within the retry window.
            if use_charger_cache and (saved_usable or _recent_failure(saved)):
                return charger, saved, "cached"
            try:
                schedule = await price_provider.get_prices(charger, force_refresh=not use_charger_cache)
            except LiveLookupUnavailable:
                # Nothing was asked of Tesla: keep a known price, otherwise leave it to the estimate.
                return charger, saved if saved_usable else None, "skipped"
            if schedule.kind not in USABLE_PRICE_KINDS and saved_usable:
                # A failed refresh must not overwrite a known price.
                return charger, saved, "cached"
            charger_knowledge.remember_pricing(charger, schedule)
            return charger, schedule, "live"

    skip = pricing_skip_events.setdefault(progress_id, asyncio.Event()) if progress_id else asyncio.Event()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.pricing_deadline_seconds
    tasks = {asyncio.create_task(one(c)): c for c in candidates}
    pending = set(tasks)
    pricing: dict[str, PricingSchedule] = {}
    completed = 0
    try:
        while pending and not skip.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            skip_wait = asyncio.create_task(skip.wait())
            done, _ = await asyncio.wait(pending | {skip_wait}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            skip_wait.cancel()
            for task in done - {skip_wait}:
                pending.discard(task)
                try:
                    charger, schedule, source = task.result()
                    if schedule is None:
                        completed += 1
                        continue
                    pricing[charger.location_id] = schedule
                    state = trip_progress.get(progress_id) if progress_id else None
                    if state is not None:
                        rows = state.setdefault("chargers", [])
                        row = next((item for item in rows if item["station_id"] == charger.location_id), None)
                        if row is not None:
                            is_manual = bool(schedule.note and schedule.note.startswith("User-entered"))
                            row.update({
                                "status": "found" if schedule.kind in USABLE_PRICE_KINDS else "unknown",
                                "source": "manual" if is_manual else source,
                                "pricing": schedule.model_dump(mode="json"),
                            })
                except Exception as exc:
                    log.warning("Pricing fetch task failed: %s", exc)
                completed += 1
                known = sum(1 for p in pricing.values() if p.kind in USABLE_PRICE_KINDS)
                _publish_progress(
                    progress_id,
                    stage="pricing",
                    message=f"Checked {completed} of {len(tasks)} charger prices; found {known}",
                    pricing_completed=completed,
                    pricing_total=len(tasks),
                    pricing_found=known,
                )
                if completed == len(tasks) or completed % 5 == 0:
                    log.info("Pricing progress: %d/%d checked, %d usable", completed, len(tasks), known)
    finally:
        for task in pending:
            task.cancel()
        if progress_id:
            pricing_skip_events.pop(progress_id, None)
    if pending:
        log.info(
            "Pricing stopped with %d of %d stations pending (%s)",
            len(pending), len(tasks), "skipped by user" if skip.is_set() else "deadline reached",
        )
    return pricing


def _plan_points(origin: Coordinate, destination: Coordinate, plan) -> list[Coordinate]:
    visits = [(s.arrival_time, s.coordinate) for s in plan.stops] + [(w.arrival_time, w.coordinate) for w in plan.waypoints]
    return [origin] + [coord for _, coord in sorted(visits, key=lambda v: v[0])] + [destination]


async def _plan_geometry(points: list[Coordinate]) -> list[list[float]]:
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
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "vehicle_groups": vehicle_cfg.profile_groups(),
            "default_vehicle_id": vehicle_cfg.DEFAULT_VEHICLE_PROFILE_ID,
            "custom_default_peak_kw": vehicle_cfg.CUSTOM_DEFAULT_PEAK_KW,
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/trip/progress/{progress_id}")
async def trip_progress_status(progress_id: str):
    state = trip_progress.get(progress_id)
    if state is None:
        return {"stage": "waiting", "message": "Waiting for trip calculation to start", "chargers": []}
    return state


@app.post("/api/trip/progress/{progress_id}/skip-pricing")
async def skip_pricing(progress_id: str):
    """Stop waiting for live prices on a running trip; pending stations use the planning estimate."""
    event = pricing_skip_events.get(progress_id)
    if event is not None:
        event.set()
    return {"ok": event is not None}


@app.post("/api/chargers/{location_id}/manual-price")
async def save_manual_price(location_id: str, req: ManualPriceRequest):
    schedule = PricingSchedule(
        kind="flat",
        bands=[{"start_minute": 0, "end_minute": 0, "price_per_kwh": req.price_per_kwh}],
        fetched_at=datetime.now(timezone.utc),
        note="User-entered planning price; not verified by Tesla",
    )
    if not charger_knowledge.remember_manual_pricing(location_id, schedule):
        raise HTTPException(status_code=404, detail="Station is not present in the local Supercharger catalog")
    cache.set("pricing", location_id, schedule.model_dump(mode="json"))
    return {"ok": True, "station_id": location_id, "pricing": schedule}


@app.post("/api/trips/{trip_id}/explain")
async def explain_trip_plan(trip_id: str, req: ExplainRequest) -> dict[str, dict[str, ChargerExplanation]]:
    graph, cfg, energy, _ = _trip_context(trip_id)
    return {"chargers": explain_plan(graph, req.plan, energy, cfg)}


@app.post("/api/trips/{trip_id}/what-if")
async def what_if_charger(trip_id: str, req: WhatIfRequest):
    graph, cfg, energy, charging = _trip_context(trip_id)
    plans, _ = optimize_departure(graph, req.plan.departure_time, energy, charging, cfg, required_station_id=req.station_id)
    if not plans:
        return {"feasible": False, "plan": None}
    best = min(plans, key=lambda p: (p.charging_cost, p.total_minutes)).model_copy(deep=True)
    best.category = "WHAT IF"
    best.route_geometry = await _plan_geometry(
        _plan_points(graph.nodes[0].coordinate, graph.nodes[graph.destination_index].coordinate, best)
    )
    return {
        "feasible": True,
        "plan": best,
        "cost_delta": round(best.charging_cost - req.plan.charging_cost, 2),
        "minutes_delta": round(best.total_minutes - req.plan.total_minutes, 1),
    }


@app.get("/api/debug/prices")
async def pricing_debug_api():
    return {"rows": cache.pricing_debug_rows()}


@app.get("/debug/prices", response_class=HTMLResponse)
async def pricing_debug_page(request: Request):
    return templates.TemplateResponse(request=request, name="prices.html", context={"rows": cache.pricing_debug_rows()})


@app.post("/api/trip", response_model=TripResponse)
async def trip(req: TripRequest) -> TripResponse:
    try:
        profile = vehicle_cfg.planning_profile(
            req.vehicle_profile_id,
            req.custom_battery_usable_kwh,
            req.custom_highway_wh_per_mile,
            req.custom_peak_charge_kw,
        )
        energy = EnergyModel(profile.battery_usable_kwh, profile.highway_wh_per_mile)
        charging = ChargingModel(profile.battery_usable_kwh, profile.charging_curve_kw)
        stop_requests = [] if req.replay_scenario else req.stops
        _publish_progress(req.progress_id, stage="geocoding", message="Resolving origin, stops and destination", chargers=[])
        resolved = await asyncio.gather(
            geocoder.geocode(req.from_location),
            *(geocoder.geocode(stop.location) for stop in stop_requests),
            geocoder.geocode(req.to_location),
        )
        origin, waypoint_locations, destination = resolved[0], list(resolved[1:-1]), resolved[-1]
        for location in resolved:
            log.info("Resolved: %s %.5f,%.5f", location.label, location.coordinate.lat, location.coordinate.lon)
        graph_waypoints = [
            GraphWaypoint(name=stop.location, coordinate=loc.coordinate, dwell_minutes=stop.dwell_minutes)
            for stop, loc in zip(stop_requests, waypoint_locations)
        ]

        _publish_progress(req.progress_id, stage="routing", message="Calculating the normal driving route")
        route_points = [loc.coordinate for loc in resolved]
        legs: list[RouteSummary] = []
        for a, b in zip(route_points, route_points[1:]):
            leg_routes = await router.route(a, b, alternatives=len(route_points) == 2)
            legs.append(min(leg_routes, key=lambda r: r.duration_minutes))
        base_route = RouteSummary(
            distance_miles=sum(leg.distance_miles for leg in legs),
            duration_minutes=sum(leg.duration_minutes for leg in legs),
            geometry=[pt for i, leg in enumerate(legs) for pt in (leg.geometry if i == 0 else leg.geometry[1:])],
        )
        log.info("Base route: %d leg(s), %.1f miles / %.0f minutes", len(legs), base_route.distance_miles, base_route.duration_minutes)

        _publish_progress(req.progress_id, stage="chargers", message="Loading the Supercharger catalog")
        all_chargers = await chargers_provider.all_open()
        charger_knowledge.remember_stations(all_chargers)
        log.info("Open Superchargers loaded: %d; filtering route corridor...", len(all_chargers))
        leg_candidates = _leg_candidates(all_chargers, legs)
        candidates = _unique_stations(leg_candidates)
        discovered_candidates = candidates
        excluded_ids = {station_id.strip() for station_id in req.excluded_station_ids if station_id.strip()}
        user_excluded = [c for c in discovered_candidates if c.location_id in excluded_ids]
        if excluded_ids:
            candidates = [c for c in discovered_candidates if c.location_id not in excluded_ids]
        log.info("Candidate chargers: %d; fetching Tesla public pricing...", len(candidates))

        _publish_progress(
            req.progress_id,
            stage="pricing",
            message=f"Found {len(discovered_candidates)} nearby chargers; fetching {len(candidates)} prices",
            candidate_total=len(discovered_candidates),
            pricing_total=len(candidates),
            pricing_completed=0,
            pricing_found=0,
            chargers=[{
                "station_id": charger.location_id,
                "station_name": charger.name,
                "tesla_url": charger.tesla_url,
                "status": "excluded" if charger.location_id in excluded_ids else "fetching",
                "source": None,
                "pricing": None,
            } for charger in discovered_candidates],
        )

        pricing = await _fetch_prices(candidates, req.use_charger_cache, req.progress_id)
        not_checked = [c for c in candidates if c.location_id not in pricing]
        replay = REPLAY_TRIPS.get(req.replay_scenario) if req.replay_scenario else None
        if replay:
            replay_rates = {station_id: rate for station_id, _, rate in replay["stations"]}
            for charger in candidates:
                if charger.location_id in replay_rates:
                    pricing[charger.location_id] = PricingSchedule(
                        kind="flat",
                        bands=[{"start_minute": 0, "end_minute": 0, "price_per_kwh": replay_rates[charger.location_id]}],
                        fetched_at=datetime.fromisoformat(replay["departure"]),
                        note="Historical observed rate used only for trip replay validation",
                    )
        for charger in candidates:
            schedule = pricing.get(charger.location_id)
            if schedule and schedule.timezone:
                charger.timezone = schedule.timezone
        verified_priced = [
            c for c in candidates
            if pricing.get(c.location_id)
            and pricing[c.location_id].kind in {"flat", "time_of_use"}
            and not (pricing[c.location_id].note or "").startswith("User-entered")
        ]
        manual_priced = [
            c for c in candidates
            if pricing.get(c.location_id)
            and pricing[c.location_id].kind in {"flat", "time_of_use"}
            and (pricing[c.location_id].note or "").startswith("User-entered")
        ]
        unknown_candidates = [c for c in candidates if c not in verified_priced and c not in manual_priced]
        unknown = len(unknown_candidates)
        estimated = 0
        if req.fallback_price_per_kwh is not None:
            estimated = len(unknown_candidates)
            for charger in unknown_candidates:
                pricing[charger.location_id] = PricingSchedule(
                    kind="estimate",
                    bands=[{"start_minute": 0, "end_minute": 0, "price_per_kwh": req.fallback_price_per_kwh}],
                    note="User-selected fallback planning rate; not a verified Tesla price",
                )
        priced_ids = {c.location_id for c in verified_priced + manual_priced}
        if req.fallback_price_per_kwh is not None:
            priced_ids |= {c.location_id for c in unknown_candidates}
        # Per-leg copies of each priced station (one station can serve several legs).
        priced = [c for c in leg_candidates if c.location_id in priced_ids]
        timezones = {c.location_id: c.timezone for c in candidates if c.timezone}
        for charger in priced:
            charger.timezone = charger.timezone or timezones.get(charger.location_id)

        # Only time-of-use pricing needs an exact station timezone. Avoid dozens of
        # unnecessary timezone API calls for flat-price or unknown-price stations.
        tou_candidates = [
            c for c in priced
            if pricing[c.location_id].kind == "time_of_use" and not c.timezone
        ]
        if tou_candidates:
            log.info("Resolving local timezones for %d time-of-use charger(s)...", len(tou_candidates))
            await _fill_timezones(tou_candidates)

        timezone_missing = _unique_stations([
            c for c in priced
            if pricing[c.location_id].kind == "time_of_use" and not c.timezone
        ])
        usable = [
            c for c in priced
            if pricing[c.location_id].kind in {"flat", "estimate"} or c.timezone
        ]
        log.info(
            "Pricing available: %d; unknown: %d; TOU timezone unavailable: %d",
            len(verified_priced), unknown, len(timezone_missing),
        )

        warnings: list[str] = []
        if price_provider.rate_limited and not price_provider.blocked:
            warnings.append(
                "The hourly limit for live Tesla price lookups was reached, so some stations use saved prices or your "
                "planning estimate. This keeps the server from being blocked by Tesla."
            )
        if price_provider.blocked:
            warnings.append(
                "Tesla is currently blocking automated price lookups from this server (\"Access Denied\"). "
                "Stations without a saved price use your planning estimate; lookups resume automatically in about 15 minutes."
            )
        if not_checked:
            warnings.append(
                f"Live prices for {len(not_checked)} station(s) weren't looked up this time, so they use your planning "
                "estimate. They're looked up again on the next trip."
            )
        # Stations not checked in time already have their own warning above.
        checked_unknown = unknown - len(not_checked)
        if checked_unknown > 0:
            if estimated:
                warnings.append(
                    f"Live Tesla pricing was unavailable for {checked_unknown} candidate Supercharger(s). "
                    f"Their costs use your ${req.fallback_price_per_kwh:.2f}/kWh planning estimate and are not live quotes."
                )
            else:
                warnings.append(
                    f"{checked_unknown} candidate Supercharger(s) had PRICE UNKNOWN and were excluded from cost optimization. "
                    "See /debug/prices for fetch details."
                )
        if timezone_missing:
            warnings.append(
                f"{len(timezone_missing)} time-of-use priced Supercharger(s) were excluded because their local "
                "timezone could not be resolved. The app will not guess UTC for local Tesla pricing windows."
            )
        if not usable and energy.reachable(req.starting_soc, base_route.distance_miles, req.destination_soc) is False:
            warnings.append("No priced Superchargers were available for a trip that requires charging, so no cost-optimal plan can be produced without inventing prices.")

        _publish_progress(req.progress_id, stage="matrix", message="Checking real driving detours to each usable charger")
        layout = graph_layout(usable, len(legs))
        stop_coordinates = [loc.coordinate for loc in waypoint_locations] + [destination.coordinate]
        matrix_points = [
            origin.coordinate if kind == "origin"
            else charger.coordinate if charger is not None
            else stop_coordinates[leg]
            for kind, leg, charger in layout
        ]
        distances, durations = await router.table(matrix_points)

        # Approximate a charger's off-route detour as half of the extra leg-start->charger->leg-end
        # time versus the fastest route for that leg. This uses road time, not straight-line distance.
        leg_ends = [i for i, (kind, _, _) in enumerate(layout) if kind in {"waypoint", "destination"}]
        leg_starts = [0] + leg_ends[:-1]
        keep_indices: list[int] = []
        kept_usable: list[Charger] = []
        detour_minutes: dict[str, float | None] = {}
        for i, (kind, leg, charger) in enumerate(layout):
            if charger is None:
                keep_indices.append(i)
                continue
            to_c = durations[leg_starts[leg]][i]
            from_c = durations[i][leg_ends[leg]]
            if to_c is None or from_c is None:
                detour_minutes.setdefault(charger.location_id, None)
                continue
            one_way_detour_estimate = max(0.0, to_c + from_c - legs[leg].duration_minutes) / 2.0
            previous = detour_minutes.get(charger.location_id)
            detour_minutes[charger.location_id] = one_way_detour_estimate if previous is None else min(previous, one_way_detour_estimate)
            if one_way_detour_estimate <= settings.max_charger_detour_minutes + 1e-6:
                keep_indices.append(i)
                kept_usable.append(charger)
        detour_excluded = {c.location_id for c in usable} - {c.location_id for c in kept_usable}
        if detour_excluded:
            warnings.append(
                f"{len(detour_excluded)} priced charger(s) were excluded by the "
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
            waypoints=graph_waypoints,
        )

        trip_id = req.progress_id or uuid.uuid4().hex
        trip_cfg = replace(
            optimizer_cfg,
            starting_soc=req.starting_soc,
            min_charger_soc=req.min_charger_soc,
            destination_soc=req.destination_soc,
        )
        _remember_trip(trip_id, graph, trip_cfg, energy, charging)
        _publish_progress(req.progress_id, stage="optimizing", message="Comparing charging plans and departure times")
        origin_tz = await timezone_provider.timezone_at(
            origin.coordinate.lat, origin.coordinate.lon
        )
        if not origin_tz:
            warnings.append(
                "Origin timezone lookup failed, so departure timestamps are shown in UTC for this run. "
                "The next-24-hour search window is still evaluated; TOU chargers without local timezones remain excluded."
            )
        departures = _departure_candidates(
            origin_tz,
            req.desired_departure_time,
            req.departure_window_hours,
        )
        all_plans = []
        departure_options = []
        states_evaluated = 0
        for departure_index, depart in enumerate(departures, start=1):
            _publish_progress(
                req.progress_id,
                stage="optimizing",
                message=f"Comparing departure {departure_index} of {len(departures)}: {depart.strftime('%a %I:%M %p')}",
                departure_completed=departure_index - 1,
                departure_total=len(departures),
            )
            plans, states = optimize_departure(
                graph,
                depart,
                energy,
                charging,
                trip_cfg,
            )
            states_evaluated += states
            all_plans.extend(plans)
            if plans:
                option = min(plans, key=lambda p: (p.charging_cost, p.total_minutes)).model_copy(deep=True)
                option.category = "DEPARTURE OPTION"
                departure_options.append(option)
        _publish_progress(
            req.progress_id,
            stage="optimizing",
            message=f"Compared all {len(departures)} departure times; selecting useful plans",
            departure_completed=len(departures),
            departure_total=len(departures),
        )
        log.info(
            "Optimization: departures tested=%d states evaluated=%d routes found=%d",
            len(departures), states_evaluated, len(all_plans),
        )

        chosen = choose_useful_plans(all_plans, base_route.distance_miles)
        for plan in chosen:
            plan.route_geometry = await _plan_geometry(_plan_points(origin.coordinate, destination.coordinate, plan))
        if chosen:
            log.info("Best route: $%.2f / %.0f min / %d stops", chosen[0].charging_cost, chosen[0].total_minutes, len(chosen[0].stops))
        else:
            warnings.append("No feasible route was found within the configured SOC and detour constraints.")

        kept_ids = {c.location_id for c in kept_usable}
        timezone_missing_ids = {c.location_id for c in timezone_missing}
        nearby_chargers: list[CandidateCharger] = []
        for charger in candidates:
            schedule = pricing.get(charger.location_id)
            price_status = (
                "historical" if schedule and schedule.note and schedule.note.startswith("Historical observed")
                else "manual" if schedule and schedule.note and schedule.note.startswith("User-entered")
                else "verified" if schedule and schedule.kind in {"flat", "time_of_use"}
                else "estimated" if schedule and schedule.kind == "estimate"
                else "unknown"
            )
            reason = None
            if charger.location_id in timezone_missing_ids:
                reason = "Local timezone unavailable for time-of-use pricing"
            elif price_status == "unknown":
                reason = "Price unavailable"
            elif charger.location_id not in kept_ids:
                reason = "Road detour exceeds the configured limit or could not be routed"
            nearby_chargers.append(CandidateCharger(
                station_id=charger.location_id,
                station_name=charger.name,
                coordinate=charger.coordinate,
                address=charger.address,
                status=charger.status,
                stalls=charger.stalls,
                power_kw=charger.power_kw,
                tesla_url=charger.tesla_url,
                route_progress=charger.route_progress,
                corridor_distance_miles=charger.corridor_distance_miles,
                detour_minutes=detour_minutes.get(charger.location_id),
                pricing_status=price_status,
                pricing=schedule,
                eligible=charger.location_id in kept_ids,
                exclusion_reason=reason,
            ))
        for charger in user_excluded:
            nearby_chargers.append(CandidateCharger(
                station_id=charger.location_id,
                station_name=charger.name,
                coordinate=charger.coordinate,
                address=charger.address,
                status=charger.status,
                stalls=charger.stalls,
                power_kw=charger.power_kw,
                tesla_url=charger.tesla_url,
                route_progress=charger.route_progress,
                corridor_distance_miles=charger.corridor_distance_miles,
                pricing_status="unknown",
                eligible=False,
                user_excluded=True,
                exclusion_reason="Excluded by user",
            ))
        nearby_chargers.sort(key=lambda c: c.route_progress)

        replay_validation = None
        if replay and departure_options:
            replay_departure = datetime.fromisoformat(replay["departure"])
            replay_plan = min(
                departure_options,
                key=lambda plan: abs((plan.departure_time - replay_departure.astimezone(plan.departure_time.tzinfo)).total_seconds()),
            )
            expected_ids = [station_id for station_id, _, _ in replay["stations"]]
            modeled_ids = [stop.station_id for stop in replay_plan.stops]
            replay_validation = {
                "label": replay["label"],
                "actual_cost": replay["actual_cost"],
                "actual_kwh": replay["actual_kwh"],
                "actual_stations": [name for _, name, _ in replay["stations"]],
                "modeled_cost": replay_plan.charging_cost,
                "modeled_kwh": replay_plan.kwh_purchased,
                "modeled_stations": [stop.station_name for stop in replay_plan.stops],
                "modeled_departure": replay_plan.departure_time.isoformat(),
                "cost_delta": round(replay_plan.charging_cost - replay["actual_cost"], 2),
                "kwh_delta": round(replay_plan.kwh_purchased - replay["actual_kwh"], 2),
                "exact_station_sequence": modeled_ids == expected_ids,
                "matched_station_count": len(set(modeled_ids) & set(expected_ids)),
            }

        response = TripResponse(
            trip_id=trip_id,
            origin=origin,
            destination=destination,
            waypoints=waypoint_locations,
            base_route=base_route,
            candidate_chargers=len(discovered_candidates),
            nearby_chargers=nearby_chargers,
            charger_cache_entries=charger_knowledge.count(),
            pricing_available=len(verified_priced),
            pricing_unknown=unknown,
            pricing_estimated=estimated,
            departures_tested=len(departures),
            plans=chosen,
            departure_options=departure_options,
            replay_validation=replay_validation,
            warnings=warnings,
            vehicle_assumptions={
                "profile_id": profile.id,
                "profile_label": profile.display_name,
                "peak_charge_kw": profile.peak_charge_kw,
                "battery_usable_kwh": profile.battery_usable_kwh,
                "highway_wh_per_mile": profile.highway_wh_per_mile,
                "estimated_highway_range_miles": round(profile.estimated_highway_range_miles, 1),
                "starting_soc": req.starting_soc,
                "min_charger_soc": req.min_charger_soc,
                "destination_soc": req.destination_soc,
                "max_preferred_charge_soc": vehicle_cfg.MAX_PREFERRED_CHARGE_SOC,
            },
        )
        _publish_progress(
            req.progress_id,
            stage="complete",
            message=f"Finished: {len(chosen)} plans found; best cost ${chosen[0].charging_cost:.2f}" if chosen else "Finished: no feasible plan found",
            chargers=[{
                "station_id": charger.station_id,
                "station_name": charger.station_name,
                "tesla_url": charger.tesla_url,
                "status": "excluded" if charger.user_excluded else "found" if charger.pricing_status == "verified" else charger.pricing_status,
                "source": None if charger.user_excluded else charger.pricing_status,
                "pricing": charger.pricing.model_dump(mode="json") if charger.pricing else None,
            } for charger in nearby_chargers],
            complete=True,
        )
        return response
    except HTTPException:
        raise
    except GeocodingNotFound as exc:
        message = f"Couldn't find \"{exc.query}\". Check the spelling, or try a city and state such as \"Hebron, KY\"."
        _publish_progress(req.progress_id, stage="error", message=message, complete=True)
        raise HTTPException(status_code=422, detail=message) from exc
    except Exception as exc:
        _publish_progress(req.progress_id, stage="error", message=f"Trip calculation failed: {type(exc).__name__}: {exc}", complete=True)
        log.exception("Trip calculation failed")
        raise HTTPException(status_code=502, detail=f"Trip calculation failed: {type(exc).__name__}: {exc}") from exc
