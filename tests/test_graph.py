import pytest

from app.models import Charger, Coordinate, PricingSchedule, PriceBand
from app.optimizer.graph import build_graph


def test_route_graph_builds_origin_chargers_destination():
    c = Charger(id="1", location_id="a", name="A", coordinate=Coordinate(lat=1,lon=1), route_progress=.5)
    p = {"a": PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0,end_minute=0,price_per_kwh=.2)])}
    d = [[0,10,20],[10,0,10],[20,10,0]]
    t = [[0,10,20],[10,0,10],[20,10,0]]
    graph = build_graph(Coordinate(lat=0,lon=0), Coordinate(lat=2,lon=2), [c], p, d, t, 20, 20)
    assert [n.kind for n in graph.nodes] == ["origin","charger","destination"]
    assert graph.nodes[1].pricing is not None


def test_route_graph_rejects_bad_matrix():
    with pytest.raises(ValueError):
        build_graph(Coordinate(lat=0,lon=0), Coordinate(lat=2,lon=2), [], {}, [[0]], [[0]], 20, 20)
