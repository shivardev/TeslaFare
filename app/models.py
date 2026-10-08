from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Coordinate(BaseModel):
    lat: float
    lon: float


class ResolvedLocation(BaseModel):
    query: str
    label: str
    coordinate: Coordinate


class RouteSummary(BaseModel):
    distance_miles: float
    duration_minutes: float
    geometry: list[list[float]] = Field(description="GeoJSON-style [lon, lat] pairs")


class Charger(BaseModel):
    id: str
    location_id: str
    name: str
    coordinate: Coordinate
    address: str = ""
    country: str = ""
    status: str = "OPEN"
    stalls: int | None = None
    power_kw: int | None = None
    route_progress: float = 0.0
    route_leg: int = 0
    corridor_distance_miles: float = 0.0
    tesla_url: str | None = None
    timezone: str | None = None


class PriceBand(BaseModel):
    start_minute: int
    end_minute: int
    price_per_kwh: float
    days: list[int] = Field(default_factory=list, description="Tesla weekday numbers, Sunday=0; empty means every day")
    # Per-minute billing: $/min by charging power tier (<=60, 60-100, 100-180, >180 kW). When set, the real
    # cost depends on how fast the car charges, and price_per_kwh is only a reference estimate for display.
    minute_rates: list[float] = Field(default_factory=list)


class PricingSchedule(BaseModel):
    kind: Literal["flat", "time_of_use", "estimate", "dynamic_unknown", "unknown"]
    currency: str = "USD"
    unit: Literal["kwh", "minute"] = "kwh"
    bands: list[PriceBand] = Field(default_factory=list)
    # Per-minute sites: extra fee per minute when the site is busy (not included in plan costs).
    congestion_per_minute: float | None = None
    source_url: str | None = None
    timezone: str | None = None
    fetched_at: datetime | None = None
    note: str | None = None


class ChargingStop(BaseModel):
    station_id: str
    station_name: str
    tesla_url: str | None = None
    arrival_time: datetime
    arrival_soc: float
    price_per_kwh: float
    price_is_estimate: bool = False
    # Billed by the minute: price_per_kwh is then the effective $/kWh for this session.
    billed_per_minute: bool = False
    kwh_purchased: float
    departure_soc: float
    charging_minutes: float
    cost: float
    coordinate: Coordinate


class WaypointVisit(BaseModel):
    """A user-requested stop between origin and destination, visited in the order given."""
    index: int
    name: str
    coordinate: Coordinate
    arrival_time: datetime
    arrival_soc: float
    dwell_minutes: float
    departure_time: datetime


class CandidateCharger(BaseModel):
    station_id: str
    station_name: str
    coordinate: Coordinate
    address: str = ""
    status: str = "OPEN"
    stalls: int | None = None
    power_kw: int | None = None
    tesla_url: str | None = None
    route_progress: float
    corridor_distance_miles: float
    detour_minutes: float | None = None
    pricing_status: Literal["verified", "historical", "manual", "captured", "estimated", "unknown"] = "unknown"
    pricing: PricingSchedule | None = None
    eligible: bool = False
    user_excluded: bool = False
    exclusion_reason: str | None = None


class TripPlan(BaseModel):
    category: str = ""
    departure_time: datetime
    arrival_time: datetime
    total_miles: float
    extra_miles_vs_fastest: float = 0.0
    driving_minutes: float
    charging_minutes: float
    dwell_minutes: float = 0.0
    total_minutes: float
    charging_cost: float
    kwh_purchased: float
    starting_soc: float | None = None
    arrival_soc: float | None = None
    stops: list[ChargingStop]
    waypoints: list[WaypointVisit] = Field(default_factory=list)
    route_geometry: list[list[float]] = Field(default_factory=list)


class TripResponse(BaseModel):
    trip_id: str | None = None
    origin: ResolvedLocation
    destination: ResolvedLocation
    waypoints: list[ResolvedLocation] = Field(default_factory=list)
    base_route: RouteSummary
    candidate_chargers: int
    nearby_chargers: list[CandidateCharger] = Field(default_factory=list)
    charger_cache_entries: int = 0
    pricing_available: int
    pricing_unknown: int
    pricing_estimated: int = 0
    departures_tested: int
    plans: list[TripPlan]
    departure_options: list[TripPlan] = Field(default_factory=list)
    replay_validation: dict | None = None
    # Station id -> version (update time) of the saved/captured price this plan used. The planner
    # compares this with the price session to know when a newer price should trigger a re-plan.
    price_versions: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    vehicle_assumptions: dict[str, float | str]
