from __future__ import annotations

import asyncio
import hmac
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

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
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
from app.pricing.collector import collection_queue, identify_station
from app.pricing.guards import (
    CommunityGate, PriceRejected, check_price_bounds, contributor_id, names_other_station, validate_payload_shape,
)
from app.pricing.refresher import PriceRefresher
from app.pricing.tesla import LiveLookupUnavailable, TeslaPriceProvider, parse_tesla_pricing_payload
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
price_refresher = PriceRefresher(
    price_provider,
    charger_knowledge,
    lambda: chargers_provider.all_open(),
    per_hour=settings.price_refresh_per_hour,
    stale_after=timedelta(days=settings.price_refresh_stale_days),
    countries={c.strip() for c in settings.price_refresh_countries.split(",") if c.strip()},
    visitor_reserve=settings.price_refresh_visitor_reserve,
)
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
    refresh_task = None
    central_task = asyncio.create_task(central_pull_loop()) if settings.central_price_url else None
    if settings.server_price_lookups and settings.price_refresh_per_hour > 0 and settings.tesla_playwright_fallback:
        refresh_task = asyncio.create_task(price_refresher.run())
    yield
    if refresh_task is not None:
        refresh_task.cancel()
    if central_task is not None:
        central_task.cancel()
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
    # Prices the visitor typed in ($/kWh by station id). Used for this trip only; never stored on the server.
    price_overrides: dict[str, float] = Field(default_factory=dict, max_length=200)
    # Tesla get-charger-details data captured by the visitor's browser helper, by station id.
    # Parsed for this trip only and never stored on the server.
    captured_prices: dict[str, dict] = Field(default_factory=dict, max_length=60)
    # The planner page's price session: the server-side record of this trip's stations and the prices
    # captured for it. Stays the same across re-plans of the same trip.
    price_session: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{8,64}$")
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
    def validate_price_overrides(self):
        for station_id, price in self.price_overrides.items():
            if len(station_id) > 120 or not 0.01 <= price <= 2.0:
                raise ValueError("Entered prices must be between $0.01 and $2.00 per kWh")
        return self

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


class CollectedPriceRequest(BaseModel):
    page_url: str = Field(default="", max_length=2000)
    request_url: str = Field(default="", max_length=4000)
    payload: dict


class ManualPriceRequest(BaseModel):
    price_per_kwh: float = Field(ge=0.01, le=2.0)


class SharedTripRequest(BaseModel):
    trip: TripResponse
    selected_plan: TripPlan
    request: dict = Field(default_factory=dict)


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
# Starts with "User-entered" so it is treated as a manual price everywhere.
VISITOR_PRICE_NOTE = "User-entered for this trip only; not shared"
CAPTURED_PRICE_NOTE = "User-entered: captured from Tesla in this visitor's browser for this trip; not shared"


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
            if not settings.server_price_lookups:
                # Collector mode: prices only come from the host's browser, never from the server.
                return charger, saved if saved_usable else None, "saved"
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


@app.get("/trip/{share_id}", response_class=HTMLResponse)
async def shared_trip_page(request: Request, share_id: uuid.UUID):
    return await home(request)


@app.post("/api/shared-trips", status_code=201)
async def create_shared_trip(payload: SharedTripRequest, request: Request):
    share_id = str(uuid.uuid4())
    allowed = {
        "from_location", "to_location", "stops", "fallback_price_per_kwh", "starting_soc",
        "min_charger_soc", "destination_soc", "vehicle_profile_id", "custom_battery_usable_kwh",
        "custom_highway_wh_per_mile", "custom_peak_charge_kw", "desired_departure_time",
        "departure_window_hours", "excluded_station_ids", "use_charger_cache",
    }
    snapshot = {
        "trip": payload.trip.model_dump(mode="json"),
        "selected_plan": payload.selected_plan.model_dump(mode="json"),
        "request": {key: value for key, value in payload.request.items() if key in allowed},
    }
    expires_at = cache.create_trip_share(share_id, snapshot, settings.trip_share_days)
    return {
        "id": share_id,
        "url": str(request.url_for("shared_trip_page", share_id=share_id)),
        "expires_at": expires_at.isoformat(),
    }


@app.get("/api/shared-trips/{share_id}")
async def get_shared_trip(share_id: uuid.UUID):
    saved = cache.get_trip_share(str(share_id))
    if saved is None:
        raise HTTPException(status_code=404, detail="This shared trip was not found or has expired.")
    snapshot, expires_at = saved
    return {**snapshot, "id": str(share_id), "expires_at": expires_at.isoformat()}


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


def _collector_keys() -> dict[str, str]:
    """key -> contributor name. COLLECTOR_KEY is the owner; CONTRIBUTOR_KEYS adds named contributors."""
    keys = {settings.collector_key: "owner"} if settings.collector_key else {}
    for entry in settings.contributor_keys.split(","):
        name, _, key = entry.strip().partition(":")
        if name and key:
            keys[key.strip()] = name.strip()
    return keys


def _require_collector_key(key: str | None) -> str | None:
    """The contributor name for a valid key. Unless REQUIRE_COLLECTOR_KEY is on, a missing or unknown key is
    fine and returns None (the caller then identifies the sender by a hashed IP)."""
    keys = _collector_keys()
    if not settings.require_collector_key:
        for known, name in keys.items():
            if key and hmac.compare_digest(key, known):
                return name
        return None
    if not keys:
        raise HTTPException(status_code=503, detail="Price collection is off: set COLLECTOR_KEY on the server.")
    for known, name in keys.items():
        if key and hmac.compare_digest(key, known):
            return name
    raise HTTPException(status_code=401, detail="Wrong collector key.")


async def _catalog_by_id() -> dict[str, Charger]:
    return {c.location_id: c for c in await chargers_provider.all_open()}


community_gate = CommunityGate(
    settings.cache_db_path.parent / "price_audit.jsonl",
    settings.community_per_hour,
    settings.community_change_threshold,
)


def _checked_schedule(payload: dict, station: Charger, catalog_ids: set[str]) -> PricingSchedule:
    """Guardrails for every captured price: Tesla's payload shape, the right station, sane bounds."""
    try:
        validate_payload_shape(payload)
        if names_other_station(payload, station.location_id, catalog_ids):
            raise PriceRejected("That data belongs to a different station")
        schedule = parse_tesla_pricing_payload(payload, findus_url_for(station))
        if schedule.kind not in USABLE_PRICE_KINDS:
            raise PriceRejected(f"No Tesla price found for {station.name} in that data")
        check_price_bounds(schedule)
    except PriceRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    schedule.fetched_at = datetime.now(timezone.utc)
    return schedule


def _contributor(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    if settings.client_ip_header:
        forwarded = request.headers.get(settings.client_ip_header, "")
        ip = forwarded.split(",")[0].strip() or ip
    return contributor_id(ip, settings.collector_key or "teslafare")


# Stations handed to an open Tesla tab recently, so parallel tabs never get the same station.
collector_leases: dict[str, float] = {}
COLLECTOR_LEASE_SECONDS = 180
collector_paused_until: float = 0.0
collector_session_saved = 0


def _collector_paused() -> datetime | None:
    if time.time() < collector_paused_until:
        return datetime.fromtimestamp(collector_paused_until, timezone.utc)
    return None


async def _forward_to_central(req: "CollectedPriceRequest") -> None:
    """Send a price collected here to the shared database too (only when CENTRAL_CONTRIBUTOR_KEY is set)."""
    if not (settings.central_price_url and settings.central_contributor_key):
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                settings.central_price_url.rstrip("/") + "/api/collector/price",
                headers={"X-Collector-Key": settings.central_contributor_key},
                json=req.model_dump(),
            )
        if response.status_code != 200:
            log.warning("Central price server answered %s: %s", response.status_code, response.text[:200])
    except Exception as exc:
        log.warning("Couldn't forward price to the central server: %s", exc)


async def pull_central_prices() -> int:
    """Merge the shared price database into this instance (newer local prices always win)."""
    if not settings.central_price_url:
        return 0
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": settings.user_agent}) as client:
        response = await client.get(settings.central_price_url.rstrip("/") + "/api/prices/export")
        response.raise_for_status()
        data = response.json()
    updated = charger_knowledge.import_prices(data.get("prices") or {}, source="central")
    log.info("Pulled shared prices from %s: %d station(s) updated", settings.central_price_url, updated)
    return updated


async def central_pull_loop() -> None:
    """Pull shared prices once per CENTRAL_PULL_HOURS. The last pull time is kept in the cache, so a restart
    doesn't pull again before it's due."""
    interval = max(1.0, settings.central_pull_hours) * 3600
    await asyncio.sleep(15)
    while True:
        last = cache.get("central_pull", "last_success_at")
        elapsed = time.time() - float(last) if last else interval
        if elapsed < interval:
            log.info("Shared prices were pulled %.1f h ago; next pull in %.1f h", elapsed / 3600, (interval - elapsed) / 3600)
            await asyncio.sleep(interval - elapsed)
            continue
        try:
            await pull_central_prices()
            cache.set("central_pull", "last_success_at", time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Couldn't pull shared prices from %s: %s", settings.central_price_url, exc)
            await asyncio.sleep(3600)  # try again in an hour
            continue
        await asyncio.sleep(interval)


@app.post("/api/collector/price")
async def collect_price(req: CollectedPriceRequest, request: Request):
    """Price data captured by a collector's userscript from a Tesla Find Us page they opened."""
    global collector_session_saved
    contributor = _require_collector_key(request.headers.get("X-Collector-Key")) or _contributor(request)
    catalog = await _catalog_by_id()
    station = identify_station(req.page_url, req.request_url, req.payload, catalog)
    if station is None:
        raise HTTPException(status_code=404, detail="Couldn't tell which station this is; open it from the /collect page.")
    schedule = _checked_schedule(req.payload, station, set(catalog))
    previous = charger_knowledge.pricing_record(station.location_id)
    charger_knowledge.remember_pricing(station, schedule, source=f"collector:{contributor}")
    community_gate.log({"station_id": station.location_id, "station": station.name, "summary": _price_summary(schedule),
                        "contributor": contributor, "kind": "key", "outcome": "accepted", "previous": previous})
    collector_leases.pop(station.location_id, None)
    collector_session_saved += 1
    asyncio.create_task(_forward_to_central(req))
    summary = _price_summary(schedule)
    log.info("Collected price for %s from %s: %s", station.name, contributor, summary)
    return {"ok": True, "station_id": station.location_id, "station_name": station.name, "kind": schedule.kind, "summary": summary}


def _price_summary(schedule: PricingSchedule) -> str:
    if schedule.unit == "minute":
        rates = sorted({rate for band in schedule.bands for rate in band.minute_rates})
        if rates:
            return f"${rates[0]:.2f}/min" if len(rates) == 1 else f"${rates[0]:.2f}\u2013{rates[-1]:.2f}/min"
    prices = sorted({band.price_per_kwh for band in schedule.bands})
    if not prices:
        return "no price"
    return f"${prices[0]:.2f}/kWh" if len(prices) == 1 else f"${prices[0]:.2f}\u2013{prices[-1]:.2f}/kWh"


# ---------- Price sessions: the one shared state for a planned trip's prices ----------
# For each planner page: the trip's stations, prices captured in that visitor's browser (never shared
# with other trips), stations a Tesla tab was just opened for, and stations whose capture failed.
PRICE_SESSION_LIMIT = 300
PRICE_SESSION_OPENING_SECONDS = 90
PRICE_SESSION_KEEP_SECONDS = 24 * 3600
price_sessions: dict[str, dict] = {}


def _save_price_session(session_id: str, session: dict) -> None:
    """Persist a session (not its short-lived "opening" marks) so it survives a restart for 24 h."""
    cache.set("price_session", session_id, {
        "stations": sorted(session["stations"]),
        "captured": {sid: [at, schedule.model_dump(mode="json")] for sid, (at, schedule) in session["captured"].items()},
        "failed": session["failed"],
    })


def _load_price_session(session_id: str) -> dict | None:
    data = cache.get("price_session", session_id, max_age_seconds=PRICE_SESSION_KEEP_SECONDS)
    if not isinstance(data, dict):
        return None
    captured = {}
    for sid, entry in (data.get("captured") or {}).items():
        try:
            captured[sid] = (entry[0], PricingSchedule.model_validate(entry[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return {"stations": set(data.get("stations") or []), "captured": captured, "opening": {}, "failed": dict(data.get("failed") or {})}


def _price_session(session_id: str, create: bool = False) -> dict | None:
    session = price_sessions.pop(session_id, None) or _load_price_session(session_id)
    if session is None:
        if not create:
            return None
        session = {"stations": set(), "captured": {}, "opening": {}, "failed": {}}
    price_sessions[session_id] = session  # most recently used last
    while len(price_sessions) > PRICE_SESSION_LIMIT:
        price_sessions.pop(next(iter(price_sessions)))
    return session


def _session_or_404(session_id: str) -> dict:
    session = _price_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="This planner session has expired; plan the trip again.")
    return session


@app.get("/api/price-sessions/{session_id}")
async def price_session_state(session_id: str):
    """Every station of the trip: priced (saved or captured), opening (a Tesla tab is open for it),
    failed (no price came back), or missing. The planner draws its panel from this and re-plans
    when a station's price changes."""
    session = _session_or_404(session_id)
    index = charger_knowledge.price_index()
    now = time.time()
    stations = {}
    for station_id in sorted(session["stations"]):
        captured = session["captured"].get(station_id)
        saved = index.get(station_id)
        if captured is not None:
            stations[station_id] = {"status": "priced", "source": "captured", "updated_at": captured[0], "summary": _price_summary(captured[1])}
        elif saved is not None and saved[1].kind in USABLE_PRICE_KINDS:
            stations[station_id] = {"status": "priced", "source": "saved", "updated_at": saved[0], "summary": _price_summary(saved[1])}
        elif session["opening"].get(station_id, 0) > now:
            stations[station_id] = {"status": "opening"}
        elif station_id in session["failed"]:
            stations[station_id] = {"status": "failed", "reason": session["failed"][station_id]}
        else:
            stations[station_id] = {"status": "missing"}
    return {"stations": stations}


class SessionStationsRequest(BaseModel):
    station_ids: list[str] = Field(max_length=20)


@app.post("/api/price-sessions/{session_id}/opening")
async def price_session_opening(session_id: str, req: SessionStationsRequest):
    """The planner opened Tesla tabs for these stations. Shown as "opening" for a while; if no price
    arrives (tab blocked or closed), they go back to missing on their own."""
    session = _session_or_404(session_id)
    until = time.time() + PRICE_SESSION_OPENING_SECONDS
    for station_id in req.station_ids:
        if station_id in session["stations"]:
            session["opening"][station_id] = until
            session["failed"].pop(station_id, None)
    return {"ok": True}


class SessionCaptureRequest(BaseModel):
    page_url: str = Field(default="", max_length=2000)
    request_url: str = Field(default="", max_length=4000)
    payload: dict | None = None
    station_id: str | None = Field(default=None, max_length=120)
    failed: str | None = Field(default=None, max_length=200)


@app.post("/api/price-sessions/{session_id}/captured")
async def price_session_captured(session_id: str, req: SessionCaptureRequest, request: Request):
    """A price the helper captured in this visitor's browser. It is used for this trip right away and,
    behind the community guardrails, offered to the shared prices. A `failed` reason marks the station as
    failed instead."""
    session = _session_or_404(session_id)
    full_catalog = await _catalog_by_id()
    catalog = {sid: c for sid, c in full_catalog.items() if sid in session["stations"]}
    station = catalog.get(req.station_id or "") or identify_station(req.page_url, req.request_url, req.payload or {}, catalog)
    if station is None:
        raise HTTPException(status_code=404, detail="That station isn't part of this trip.")
    session["opening"].pop(station.location_id, None)
    if req.failed or req.payload is None:
        session["failed"][station.location_id] = req.failed or "No price came back"
        _save_price_session(session_id, session)
        return {"ok": False, "station_name": station.name}
    try:
        schedule = _checked_schedule(req.payload, station, set(full_catalog))
    except HTTPException as exc:
        session["failed"][station.location_id] = exc.detail
        _save_price_session(session_id, session)
        raise
    session["failed"].pop(station.location_id, None)
    session["captured"][station.location_id] = (datetime.now(timezone.utc).isoformat(), schedule)
    _save_price_session(session_id, session)
    shared = _share_community_price(station, schedule, request)
    return {"ok": True, "station_name": station.name, "summary": _price_summary(schedule), "shared": shared}


def _share_community_price(station: Charger, schedule: PricingSchedule, request: Request) -> str:
    """Offer a visitor's price to the shared prices. Key holders' helpers save through /api/collector/price,
    so their requests are skipped here."""
    key = request.headers.get("X-Collector-Key")
    if not settings.community_prices or (key and any(hmac.compare_digest(key, k) for k in _collector_keys())):
        return "skipped"
    contributor = _contributor(request)
    saved = charger_knowledge.pricing(station.location_id)
    saved = saved if saved is not None and saved.kind in USABLE_PRICE_KINDS else None
    outcome = community_gate.decide(station.location_id, schedule, contributor, saved)
    previous = charger_knowledge.pricing_record(station.location_id)
    if outcome == "accepted":
        charger_knowledge.remember_pricing(station, schedule, source=f"community:{contributor}")
    community_gate.log({"station_id": station.location_id, "station": station.name, "summary": _price_summary(schedule),
                        "contributor": contributor, "kind": "community", "outcome": outcome,
                        "previous": previous if outcome == "accepted" else None})
    return outcome


@app.get("/api/collector/audit")
async def price_audit(request: Request, limit: int = 200):
    """Recent price submissions (owner/contributor keys only)."""
    _require_collector_key(request.headers.get("X-Collector-Key"))
    return {"entries": community_gate.recent(max(1, min(limit, 1000)))}


class RevertRequest(BaseModel):
    contributor: str = Field(min_length=1, max_length=64)


@app.post("/api/collector/revert")
async def revert_contributor(req: RevertRequest, request: Request):
    """Undo every price a contributor got accepted, where it is still their price (owner/contributor keys)."""
    name = _require_collector_key(request.headers.get("X-Collector-Key")) or _contributor(request)
    restored = 0
    for entry in reversed(community_gate.accepted_by(req.contributor)):
        current = charger_knowledge.pricing_record(entry["station_id"])
        if current.get("pricing_source") in (f"community:{req.contributor}", f"collector:{req.contributor}"):
            charger_knowledge.restore_pricing(entry["station_id"], entry.get("previous") or {})
            restored += 1
    community_gate.log({"contributor": req.contributor, "kind": "revert", "outcome": f"reverted {restored}", "by": name})
    return {"ok": True, "restored": restored}


def _price_versions(chargers: list[Charger], session: dict | None) -> dict[str, str]:
    """The same versions /api/price-sessions reports, for the prices this plan used."""
    index = charger_knowledge.price_index()
    versions = {}
    for charger in chargers:
        captured = session["captured"].get(charger.location_id) if session else None
        saved = index.get(charger.location_id)
        if captured is not None:
            versions[charger.location_id] = captured[0]
        elif saved is not None and saved[1].kind in USABLE_PRICE_KINDS:
            versions[charger.location_id] = saved[0]
    return versions


def findus_url_for(station: Charger) -> str:
    return station.tesla_url or f"https://www.tesla.com/findus?location={station.location_id}"


async def _queue_items() -> tuple[list[dict], dict[str, int]]:
    return collection_queue(
        await chargers_provider.all_open(),
        charger_knowledge.all_pricing(),
        price_refresher.recent_trip_ids(),
        datetime.now(timezone.utc),
        timedelta(hours=settings.price_fresh_hours),
        {c.strip() for c in settings.price_refresh_countries.split(",") if c.strip()},
    )


@app.get("/api/collector/queue")
async def price_queue(limit: int = 60):
    items, counts = await _queue_items()
    paused = _collector_paused()
    return {
        "items": items[: max(1, min(limit, 200))],
        "counts": counts,
        "fresh_hours": settings.price_fresh_hours,
        "collector_enabled": bool(_collector_keys()) or not settings.require_collector_key,
        "key_required": settings.require_collector_key,
        "paused_until": paused.isoformat() if paused else None,
    }


@app.get("/api/collector/next")
async def next_stations(request: Request, count: int = 1):
    """The next stations to open, leased for a few minutes so parallel tabs don't repeat a station."""
    _require_collector_key(request.headers.get("X-Collector-Key"))
    paused = _collector_paused()
    if paused:
        return {"items": [], "paused_until": paused.isoformat(), "remaining": None, "saved_this_session": collector_session_saved}
    now = time.time()
    for station_id, expires in list(collector_leases.items()):
        if expires < now:
            collector_leases.pop(station_id, None)
    items, counts = await _queue_items()
    picked = [item for item in items if item["station_id"] not in collector_leases][: max(1, min(count, 5))]
    for item in picked:
        collector_leases[item["station_id"]] = now + COLLECTOR_LEASE_SECONDS
    return {
        "items": picked,
        "paused_until": None,
        "remaining": counts["stale"] + counts["missing"],
        "saved_this_session": collector_session_saved,
    }


class CollectorStationRequest(BaseModel):
    station_id: str = Field(min_length=1, max_length=120)


@app.post("/api/collector/skip")
async def skip_station(req: CollectorStationRequest, request: Request):
    """The collector couldn't get a price here; mark it tried so it drops down the queue for a day."""
    _require_collector_key(request.headers.get("X-Collector-Key"))
    station = (await _catalog_by_id()).get(req.station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Unknown station.")
    if charger_knowledge.pricing(station.location_id) is None or charger_knowledge.pricing(station.location_id).kind not in USABLE_PRICE_KINDS:
        charger_knowledge.remember_pricing(
            station,
            PricingSchedule(kind="unknown", note="Skipped by collector", fetched_at=datetime.now(timezone.utc)),
        )
    collector_leases.pop(station.location_id, None)
    return {"ok": True}


@app.post("/api/collector/blocked")
async def collector_blocked(request: Request):
    """A collector's tab got Tesla's "Access Denied": pause collection instead of pushing through it."""
    global collector_paused_until
    _require_collector_key(request.headers.get("X-Collector-Key"))
    collector_paused_until = time.time() + settings.collector_pause_minutes * 60
    log.warning("Collector reported Tesla Access Denied; pausing collection for %s minutes", settings.collector_pause_minutes)
    return {"ok": True, "paused_until": _collector_paused().isoformat()}


@app.get("/api/prices/status")
async def price_status(ids: str = ""):
    """When each of these stations' saved Tesla price last changed. Open planner tabs poll this and
    re-plan when a station on their route gets a new or updated price."""
    wanted = [i for i in ids.split(",")[:100] if i]
    index = charger_knowledge.price_index()
    return {"prices": {
        i: index[i][0] for i in wanted
        if i in index and index[i][1].kind in USABLE_PRICE_KINDS
    }}


@app.get("/api/prices/export")
async def export_prices():
    """Every collected Tesla price, for other TeslaFare instances to pull."""
    return charger_knowledge.export_prices()


@app.get("/collect", response_class=HTMLResponse)
async def collect_page(request: Request):
    return templates.TemplateResponse(request=request, name="collect.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/collector.user.js", response_class=PlainTextResponse)
@app.get("/collector/{key}/teslafare.user.js", response_class=PlainTextResponse)
async def collector_userscript(request: Request, key: str = ""):
    """The collector userscript for this server. With a valid ?key= it is built in, so the script never asks."""
    if key:
        _require_collector_key(key)
    server = (settings.public_url or str(request.base_url)).rstrip("/")
    host = httpx.URL(server).host or "localhost"
    script = (BASE_DIR / "static" / "collector.user.js").read_text(encoding="utf-8")
    script = script.replace("__SERVER__", server).replace("__HOST__", host).replace("__KEY__", key.replace("'", ""))
    return PlainTextResponse(script, media_type="text/javascript", headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/chargers/{location_id}/manual-price")
async def save_manual_price(location_id: str, req: ManualPriceRequest):
    if not settings.allow_shared_manual_prices:
        raise HTTPException(status_code=403, detail="Shared manual prices are turned off on this server.")
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

        price_refresher.note_trip_stations([c.location_id for c in candidates])
        pricing = await _fetch_prices(candidates, req.use_charger_cache, req.progress_id)
        session = _price_session(req.price_session, create=True) if req.price_session else None
        if session is not None:
            session["stations"] = {c.location_id for c in discovered_candidates}
            _save_price_session(req.price_session, session)
            for charger in candidates:
                entry = session["captured"].get(charger.location_id)
                if entry is not None:
                    pricing[charger.location_id] = entry[1].model_copy(update={"note": CAPTURED_PRICE_NOTE})
        for charger in candidates:
            captured = req.captured_prices.get(charger.location_id)
            if captured:
                schedule = parse_tesla_pricing_payload(captured, findus_url_for(charger))
                if schedule.kind in USABLE_PRICE_KINDS:
                    schedule.note = CAPTURED_PRICE_NOTE
                    pricing[charger.location_id] = schedule
        for charger in candidates:
            entered = req.price_overrides.get(charger.location_id)
            if entered is not None:
                pricing[charger.location_id] = PricingSchedule(
                    kind="flat",
                    bands=[{"start_minute": 0, "end_minute": 0, "price_per_kwh": entered}],
                    note=VISITOR_PRICE_NOTE,
                )
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
                f"{len(not_checked)} station(s) don't have a collected price yet, so they use your planning estimate."
                if not settings.server_price_lookups else
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
                else "captured" if schedule and schedule.note == CAPTURED_PRICE_NOTE
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
            price_versions=_price_versions(discovered_candidates, session),
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
