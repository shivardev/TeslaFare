import math

from app.chargers.supercharge_info import SuperchargeInfoProvider
from app.models import Charger, Coordinate


def test_corridor_filter_keeps_route_nearby_and_caps_candidates():
    geometry = [[-85.3 + i * 0.01, 35.0 + i * 0.01] for i in range(500)]
    chargers = []
    for i in range(100):
        # Near the route.
        chargers.append(
            Charger(
                id=f"near-{i}",
                location_id=f"near-{i}",
                name=f"near-{i}",
                coordinate=Coordinate(lat=35.0 + i * 0.03, lon=-85.3 + i * 0.03),
            )
        )
    for i in range(100):
        # Very far away and should be discarded cheaply by the bounding box.
        chargers.append(
            Charger(
                id=f"far-{i}",
                location_id=f"far-{i}",
                name=f"far-{i}",
                coordinate=Coordinate(lat=45.0, lon=-110.0 + i * 0.01),
            )
        )

    out = SuperchargeInfoProvider.corridor_candidates(chargers, geometry, 50, 24)
    assert 0 < len(out) <= 24
    assert all(c.id.startswith("near-") for c in out)
    assert all(math.isfinite(c.corridor_distance_miles) for c in out)


def test_capped_candidates_cover_the_whole_route_including_the_end():
    # Chargers every few miles along the whole route, like a real interstate. An earlier version
    # took two per route slice and then cut the list to the cap in route order, silently dropping
    # the last ~30% of every long trip.
    geometry = [[-85.0 + i * 0.01, 35.0] for i in range(1000)]  # ~570 miles due east
    chargers = [
        Charger(id=f"c-{i}", location_id=f"c-{i}", name=f"c-{i}",
                coordinate=Coordinate(lat=35.0 + (i % 3) * 0.02, lon=-85.0 + i * 0.1))
        for i in range(100)
    ]
    picked = SuperchargeInfoProvider.corridor_candidates(chargers, geometry, 50, 24)
    assert len(picked) == 24
    progresses = [c.route_progress for c in picked]
    assert max(progresses) > 0.9
    assert max(b - a for a, b in zip(progresses, progresses[1:])) < 0.1
