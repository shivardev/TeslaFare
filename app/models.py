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
    status: str = "OPEN"
    stalls: int | None = None
    power_kw: int | None = None
    route_progress: float = 0.0
    corridor_distance_miles: float = 0.0
    tesla_url: str | None = None
    timezone: str | None = None


class PriceBand(BaseModel):
    start_minute: int
    end_minute: int
    price_per_kwh: float


class PricingSchedule(BaseModel):
    kind: Literal["flat", "time_of_use", "dynamic_unknown", "unknown"]
    currency: str = "USD"
    bands: list[PriceBand] = Field(default_factory=list)
    source_url: str | None = None
    fetched_at: datetime | None = None
    note: str | None = None


class ChargingStop(BaseModel):
    station_id: str
    station_name: str
    tesla_url: str | None = None
    arrival_time: datetime
    arrival_soc: float
    price_per_kwh: float
    kwh_purchased: float
    departure_soc: float
    charging_minutes: float
    cost: float
    coordinate: Coordinate


class TripPlan(BaseModel):
    category: str = ""
    departure_time: datetime
    arrival_time: datetime
    total_miles: float
    extra_miles_vs_fastest: float = 0.0
    driving_minutes: float
    charging_minutes: float
    total_minutes: float
    charging_cost: float
    kwh_purchased: float
    stops: list[ChargingStop]
    route_geometry: list[list[float]] = Field(default_factory=list)


class TripResponse(BaseModel):
    origin: ResolvedLocation
    destination: ResolvedLocation
    base_route: RouteSummary
    candidate_chargers: int
    pricing_available: int
    pricing_unknown: int
    departures_tested: int
    plans: list[TripPlan]
    warnings: list[str] = Field(default_factory=list)
    vehicle_assumptions: dict[str, float]
