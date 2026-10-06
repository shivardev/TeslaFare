from datetime import datetime, timedelta, timezone

from app.models import Charger, Coordinate, PriceBand, PricingSchedule
from app.optimizer.explain import explain_plan
from app.optimizer.graph import build_graph
from app.optimizer.search import OptimizerConfig, optimize_departure
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel

ENERGY = EnergyModel(60, 300)  # 0.5 SOC point per mile
CHARGING = ChargingModel(60, [(0, 100, 100)])
# 9:00 PM UTC departure: arrives at the chargers (90 miles = 90 min) at 10:30 PM.
DEPART = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
CFG = OptimizerConfig(
    starting_soc=60, min_charger_soc=10, destination_soc=10, max_preferred_charge_soc=85,
    absolute_max_charge_soc=100, max_route_detour_percent=30, max_total_extra_driving_minutes=120,
    results_per_departure=5,
)


def charger(cid, progress, schedule):
    return Charger(id=cid, location_id=cid, name=cid, coordinate=Coordinate(lat=progress, lon=0),
                   route_progress=progress, timezone="UTC", tesla_url="x"), schedule


def flat(price):
    return PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=price)])


# Meijer Way's real schedule: $0.44 from 8 AM to 11 PM, $0.23 overnight.
MEIJER = PricingSchedule(kind="time_of_use", timezone="UTC", bands=[
    PriceBand(start_minute=480, end_minute=1380, price_per_kwh=.44),
    PriceBand(start_minute=1380, end_minute=480, price_per_kwh=.23),
])


def lexington_graph():
    flat_c, flat_p = charger("LEXINGTON", .45, flat(.39))
    tou_c, tou_p = charger("MEIJER", .46, MEIJER)
    # origin, LEXINGTON, MEIJER, destination. MEIJER is a few minutes off route.
    d = [[0, 90, 92, 200], [90, 0, 4, 110], [92, 4, 0, 112], [200, 110, 112, 0]]
    t = [[0, 90, 95, 200], [90, 0, 6, 110], [95, 6, 0, 114], [200, 110, 114, 0]]
    return build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=1, lon=0), [flat_c, tou_c],
                       {"LEXINGTON": flat_p, "MEIJER": tou_p}, d, t, 200, 200)


def test_explains_peak_price_and_when_it_drops():
    graph = lexington_graph()
    plans, _ = optimize_departure(graph, DEPART, ENERGY, CHARGING, CFG)
    best = plans[0]
    assert [s.station_id for s in best.stops] == ["LEXINGTON"]
    why = explain_plan(graph, best, ENERGY, CFG)
    assert why["LEXINGTON"].verdict == "used"
    meijer = why["MEIJER"]
    assert meijer.verdict == "pricier"
    assert meijer.price_at_pass == .44
    assert meijer.ref_station_name == "LEXINGTON" and meijer.ref_price == .39
    # Going there instead of charging at LEXINGTON: arrive LEXINGTON at +90 min with 15%, then 6 min / 4 miles.
    assert meijer.pass_time == DEPART + timedelta(minutes=96)
    assert meijer.pass_soc == 13
    assert meijer.cheaper_price == .23
    assert meijer.cheaper_at == datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
    # Miles from the last charge point: LEXINGTON is 4 road miles before MEIJER; LEXINGTON is 90 from the start.
    assert (meijer.miles_from_last_charge, meijer.last_charge_name, meijer.trip_miles) == (4, "LEXINGTON", 94)
    lex = why["LEXINGTON"]
    assert (lex.miles_from_last_charge, lex.last_charge_name, lex.trip_miles) == (90, "Start", 90)


def test_required_station_forces_a_charge_there():
    graph = lexington_graph()
    best = optimize_departure(graph, DEPART, ENERGY, CHARGING, CFG)[0][0]
    forced, _ = optimize_departure(graph, DEPART, ENERGY, CHARGING, CFG, required_station_id="MEIJER")
    assert forced
    assert all("MEIJER" in [s.station_id for s in p.stops] for p in forced)
    assert min(p.charging_cost for p in forced) > best.charging_cost


def test_unneeded_charger_is_reported_as_not_needed():
    c, p = charger("A", .3, flat(.20))
    d = [[0, 30, 100], [30, 0, 70], [100, 70, 0]]
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=1, lon=0), [c], {"A": p}, d, d, 100, 100)
    best = optimize_departure(graph, DEPART, ENERGY, CHARGING, CFG)[0][0]
    assert best.stops == []
    assert explain_plan(graph, best, ENERGY, CFG)["A"].verdict == "not_needed"


def test_unknown_station_cannot_be_required():
    assert optimize_departure(lexington_graph(), DEPART, ENERGY, CHARGING, CFG, required_station_id="NOPE") == ([], 0)


def test_charger_passed_with_plenty_of_battery_is_too_early():
    # A cheap-enough charger 10 miles in (95% battery) before the plan's real stop.
    early, p_early = charger("EARLY", .05, flat(.45))
    flat_c, flat_p = charger("LEXINGTON", .45, flat(.39))
    d = [[0, 10, 90, 200], [10, 0, 80, 190], [90, 80, 0, 110], [200, 190, 110, 0]]
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=1, lon=0), [early, flat_c],
                        {"EARLY": p_early, "LEXINGTON": flat_p}, d, d, 200, 200)
    best = optimize_departure(graph, DEPART, ENERGY, CHARGING, CFG)[0][0]
    why = explain_plan(graph, best, ENERGY, CFG)["EARLY"]
    assert why.verdict == "too_early" and why.pass_soc == 55
