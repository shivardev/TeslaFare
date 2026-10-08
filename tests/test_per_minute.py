"""Per-minute Supercharger pricing, from Tesla's real response for Murfreesboro, TN."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import vehicle as v
from app.models import Charger, Coordinate
from app.optimizer.graph import build_graph
from app.optimizer.search import OptimizerConfig, optimize_departure
from app.pricing.base import minute_rate_for_power, session_cost
from app.pricing.guards import check_price_bounds, validate_payload_shape
from app.pricing.tesla import parse_tesla_pricing_payload
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel

PAYLOAD = json.loads((Path(__file__).parent / "data" / "murfreesboro_per_minute.json").read_text())


def test_parses_tesla_member_per_minute_tiers_and_congestion():
    schedule = parse_tesla_pricing_payload(PAYLOAD, "x")
    assert schedule.kind == "flat" and schedule.unit == "minute"
    assert schedule.bands[0].minute_rates == [0.26, 0.51, 0.82, 1.37]  # Tesla & members, not the non-Tesla rows
    assert schedule.congestion_per_minute == 0.5
    assert schedule.timezone == "America/Chicago"
    validate_payload_shape(PAYLOAD)
    check_price_bounds(schedule)


def test_tier_by_charging_power():
    rates = [0.26, 0.51, 0.82, 1.37]
    assert [minute_rate_for_power(rates, kw) for kw in (50, 60, 61, 100, 150, 180, 181, 250)] == [0.26, 0.26, 0.51, 0.51, 0.82, 0.82, 1.37, 1.37]


def test_session_cost_follows_the_cars_curve():
    band = parse_tesla_pricing_payload(PAYLOAD, "x").bands[0]
    car = v.planning_profile("model_y_standard_2026")  # peaks ~165 kW: the 100-180 kW tier
    charging = ChargingModel(car.battery_usable_kwh, car.charging_curve_kw)
    expected = sum(m * minute_rate_for_power(band.minute_rates, kw) for m, kw in charging.segments(10, 80))
    cost = session_cost(charging, 10, 80, band)
    assert abs(cost - expected) < 1e-9
    effective = cost / charging.kwh_between(10, 80)
    assert 0.25 < effective < 0.45  # roughly $0.30-0.40/kWh for this car, not the $0.09 the lowest tier would suggest


def test_optimizer_costs_per_minute_stop_and_marks_it():
    schedule = parse_tesla_pricing_payload(PAYLOAD, "x")
    station = Charger(id="M", location_id="M", name="Murfreesboro", coordinate=Coordinate(lat=0.5, lon=0),
                      route_progress=0.5, timezone="UTC", tesla_url="x")
    d = [[0, 120, 240], [120, 0, 120], [240, 120, 0]]
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=1, lon=0), [station], {"M": schedule}, d, d, 240, 240)
    cfg = OptimizerConfig(starting_soc=85, min_charger_soc=10, destination_soc=10, max_preferred_charge_soc=85,
                          absolute_max_charge_soc=100, max_route_detour_percent=30, max_total_extra_driving_minutes=120)
    energy = EnergyModel(60, 300)
    charging = ChargingModel(60, v.CURVE_MY_STANDARD_175)
    plans, _ = optimize_departure(graph, datetime(2026, 10, 8, 12, tzinfo=timezone.utc), energy, charging, cfg)
    stop = plans[0].stops[0]
    assert stop.billed_per_minute
    band = schedule.bands[0]
    assert abs(stop.cost - round(session_cost(charging, stop.arrival_soc, stop.departure_soc, band), 2)) < 0.02
    assert abs(stop.price_per_kwh - stop.cost / stop.kwh_purchased) < 0.01  # effective $/kWh shown in the UI
