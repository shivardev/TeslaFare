from datetime import datetime, timezone

from app.models import Charger, Coordinate, PriceBand, PricingSchedule
from app.optimizer.graph import build_graph
from app.optimizer.search import OptimizerConfig, optimize_departure
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel


def flat(price):
    return PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0,end_minute=0,price_per_kwh=price)])


def cfg():
    return OptimizerConfig(
        starting_soc=100,
        min_charger_soc=10,
        destination_soc=10,
        max_preferred_charge_soc=85,
        absolute_max_charge_soc=100,
        max_route_detour_percent=30,
        max_total_extra_driving_minutes=120,
        results_per_departure=5,
    )


def charger(cid, name, progress, price):
    return Charger(id=cid, location_id=cid, name=name, coordinate=Coordinate(lat=progress, lon=progress), route_progress=progress, timezone="UTC", tesla_url=f"https://tesla/{cid}"), flat(price)


def test_optimizer_chooses_cheaper_slightly_farther_charger():
    a, pa = charger("A","Cheap farther",.45,.20)
    b, pb = charger("B","Expensive closer",.50,.50)
    # origin,A,B,destination. Non-useful cross edges intentionally large.
    D = [
        [0,110,100,210],
        [110,0,90,100],
        [100,90,0,100],
        [210,100,100,0],
    ]
    T = [[x for x in row] for row in D]
    graph = build_graph(Coordinate(lat=0,lon=0), Coordinate(lat=1,lon=1), [a,b], {"A":pa,"B":pb}, D,T,200,200)
    energy = EnergyModel(60,300)  # 0.5 SOC point per mile
    charging = ChargingModel(60, [(0,100,100)])
    plans,_ = optimize_departure(graph, datetime(2026,1,1,tzinfo=timezone.utc), energy, charging, cfg())
    assert plans
    assert plans[0].stops[0].station_id == "A"
    assert plans[0].charging_cost < 3.0


def test_optimizer_charges_more_at_cheap_a_to_skip_expensive_b():
    a, pa = charger("A","Cheap A",.30,.20)
    b, pb = charger("B","Expensive B",.50,.48)
    c, pc = charger("C","Cheap C",.70,.21)
    # origin,A,B,C,dest
    X = 999.0
    D = [
        [0,100,150,200,280],
        [100,0,50,100,200],
        [150,50,0,50,130],
        [200,100,50,0,80],
        [280,200,130,80,0],
    ]
    T = [[x if x < X else None for x in row] for row in D]
    graph = build_graph(Coordinate(lat=0,lon=0), Coordinate(lat=1,lon=1), [a,b,c], {"A":pa,"B":pb,"C":pc}, D,T,280,280)
    energy = EnergyModel(60,300)
    charging = ChargingModel(60, [(0,100,100)])
    plans,_ = optimize_departure(graph, datetime(2026,1,1,tzinfo=timezone.utc), energy, charging, cfg())
    assert plans
    ids = [s.station_id for s in plans[0].stops]
    assert "A" in ids and "C" in ids
    assert "B" not in ids
    stop_a = next(s for s in plans[0].stops if s.station_id == "A")
    assert stop_a.departure_soc >= 60.0  # enough to make the 100-mile jump to C with reserve


def test_departure_time_changes_tou_cost():
    c = Charger(id="A", location_id="A", name="TOU", coordinate=Coordinate(lat=.5,lon=.5), route_progress=.5, timezone="UTC", tesla_url="x")
    schedule = PricingSchedule(kind="time_of_use", bands=[
        PriceBand(start_minute=0,end_minute=8*60,price_per_kwh=.20),
        PriceBand(start_minute=8*60,end_minute=0,price_per_kwh=.50),
    ])
    D = [[0,100,200],[100,0,100],[200,100,0]]
    T = [[0,60,120],[60,0,60],[120,60,0]]
    graph = build_graph(Coordinate(lat=0,lon=0), Coordinate(lat=1,lon=1), [c], {"A":schedule}, D,T,200,120)
    energy = EnergyModel(60,300)
    charging = ChargingModel(60, [(0,100,100)])
    night,_ = optimize_departure(graph, datetime(2026,1,1,1,0,tzinfo=timezone.utc), energy, charging, cfg())
    day,_ = optimize_departure(graph, datetime(2026,1,1,10,0,tzinfo=timezone.utc), energy, charging, cfg())
    assert night and day
    assert night[0].charging_cost < day[0].charging_cost
