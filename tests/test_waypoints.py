from datetime import datetime, timedelta, timezone

from app.models import Charger, Coordinate, PriceBand, PricingSchedule
from app.optimizer.graph import GraphWaypoint, build_graph, graph_layout
from app.optimizer.search import OptimizerConfig, optimize_departure
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENERGY = EnergyModel(60, 300)  # 0.5 SOC point per mile
CHARGING = ChargingModel(60, [(0, 100, 100)])


def cfg(starting_soc=100):
    return OptimizerConfig(
        starting_soc=starting_soc,
        min_charger_soc=10,
        destination_soc=10,
        max_preferred_charge_soc=85,
        absolute_max_charge_soc=100,
        max_route_detour_percent=30,
        max_total_extra_driving_minutes=120,
        results_per_departure=5,
    )


def flat(price):
    return PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=price)])


def charger(cid, leg, local_progress, price):
    c = Charger(
        id=cid, location_id=cid, name=cid, coordinate=Coordinate(lat=leg + local_progress, lon=0),
        route_progress=leg + local_progress, route_leg=leg, timezone="UTC", tesla_url=f"https://tesla/{cid}",
    )
    return c, flat(price)


def matrix(positions):
    """Road distance = |position difference| * 100 miles; durations equal distances in minutes."""
    d = [[abs(a - b) * 100 for b in positions] for a in positions]
    return d, [row[:] for row in d]


def test_layout_puts_each_stop_after_its_leg_chargers():
    a, _ = charger("A", 0, .5, .3)
    b, _ = charger("B", 1, .5, .3)
    kinds = [(kind, leg, c.location_id if c else None) for kind, leg, c in graph_layout([b, a], 2)]
    assert kinds == [
        ("origin", 0, None), ("charger", 0, "A"), ("waypoint", 0, None),
        ("charger", 1, "B"), ("destination", 1, None),
    ]


def test_plan_visits_stop_in_order_and_adds_dwell():
    # origin -> stop -> destination; 80 miles per leg, no charging needed from 100%.
    d, t = matrix([0, .8, 1.6])
    stop = GraphWaypoint(name="Lexington, KY", coordinate=Coordinate(lat=1, lon=0), dwell_minutes=45)
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=2, lon=0), [], {}, d, t, 200, 200, waypoints=[stop])
    plans, _ = optimize_departure(graph, START, ENERGY, CHARGING, cfg())
    assert plans
    plan = plans[0]
    assert [w.name for w in plan.waypoints] == ["Lexington, KY"]
    visit = plan.waypoints[0]
    assert visit.arrival_time == START + timedelta(minutes=80)
    assert visit.departure_time == START + timedelta(minutes=125)
    assert visit.arrival_soc == 60
    assert plan.dwell_minutes == 45
    assert plan.total_minutes == 205
    assert plan.arrival_time == START + timedelta(minutes=205)


def test_cheap_charger_on_later_leg_cannot_be_used_before_the_stop():
    # A cheap charger near the destination must not be reached before visiting the stop,
    # even though driving there first would be geometrically "on the way" in progress terms.
    cheap, p_cheap = charger("CHEAP", 1, .5, .10)
    pricey, p_pricey = charger("PRICEY", 0, .5, .60)
    d, t = matrix([0, .5, 1, 1.5, 2])  # origin, PRICEY, stop, CHEAP, destination
    stop = GraphWaypoint(name="Stop B", coordinate=Coordinate(lat=1, lon=0))
    graph = build_graph(
        Coordinate(lat=0, lon=0), Coordinate(lat=2, lon=0), [cheap, pricey], {"CHEAP": p_cheap, "PRICEY": p_pricey},
        d, t, 200, 200, waypoints=[stop],
    )
    plans, _ = optimize_departure(graph, START, ENERGY, CHARGING, cfg(starting_soc=60))
    assert plans
    for plan in plans:
        assert len(plan.waypoints) == 1
        events = sorted(
            [(s.arrival_time, s.station_id) for s in plan.stops] + [(w.arrival_time, "STOP") for w in plan.waypoints]
        )
        order = [name for _, name in events]
        if "CHEAP" in order:
            assert order.index("CHEAP") > order.index("STOP")
        if "PRICEY" in order:
            assert order.index("PRICEY") < order.index("STOP")


def test_stops_are_never_skipped_or_reordered():
    # Out-and-back: origin(0) -> B(.5) -> C(0 again) -> destination(.5). The direct origin->destination
    # edge is short, but the plan must still visit B then C.
    positions = [0, .5, 0, .5]
    d, t = matrix(positions)
    stops = [
        GraphWaypoint(name="B", coordinate=Coordinate(lat=1, lon=0)),
        GraphWaypoint(name="C", coordinate=Coordinate(lat=0, lon=0)),
    ]
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=1, lon=0), [], {}, d, t, 150, 150, waypoints=stops)
    plans, _ = optimize_departure(graph, START, ENERGY, CHARGING, cfg())
    assert plans
    for plan in plans:
        assert [w.name for w in plan.waypoints] == ["B", "C"]
        assert plan.total_miles == 150


def test_stop_arrival_respects_destination_reserve():
    # 190 miles to the stop needs 95 SOC points; with 100% start the car arrives at 5% < 10% reserve,
    # so it must charge at the leg-0 charger before the stop.
    a, pa = charger("A", 0, .5, .30)
    d, t = matrix([0, .95, 1.9, 2.0])  # origin, A, stop, destination
    stop = GraphWaypoint(name="Stop", coordinate=Coordinate(lat=1, lon=0))
    graph = build_graph(Coordinate(lat=0, lon=0), Coordinate(lat=2, lon=0), [a], {"A": pa}, d, t, 200, 200, waypoints=[stop])
    plans, _ = optimize_departure(graph, START, ENERGY, CHARGING, cfg())
    assert plans
    assert plans[0].stops and plans[0].stops[0].station_id == "A"
    assert plans[0].waypoints[0].arrival_soc >= 10
