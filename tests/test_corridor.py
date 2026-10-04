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
