from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models import Charger, Coordinate, PricingSchedule


@dataclass(frozen=True)
class TripNode:
    id: str
    name: str
    kind: Literal["origin", "charger", "destination"]
    coordinate: Coordinate
    progress: float
    charger: Charger | None = None
    pricing: PricingSchedule | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class GraphContext:
    nodes: list[TripNode]
    distances_miles: list[list[float | None]]
    durations_minutes: list[list[float | None]]
    base_distance_miles: float
    base_duration_minutes: float

    @property
    def destination_index(self) -> int:
        return len(self.nodes) - 1


def build_graph(
    origin: Coordinate,
    destination: Coordinate,
    chargers: list[Charger],
    pricing: dict[str, PricingSchedule],
    distances_miles: list[list[float | None]],
    durations_minutes: list[list[float | None]],
    base_distance_miles: float,
    base_duration_minutes: float,
) -> GraphContext:
    nodes = [TripNode("origin", "Origin", "origin", origin, 0.0)]
    for charger in sorted(chargers, key=lambda c: c.route_progress):
        nodes.append(
            TripNode(
                id=charger.location_id,
                name=charger.name,
                kind="charger",
                coordinate=charger.coordinate,
                progress=charger.route_progress,
                charger=charger,
                pricing=pricing.get(charger.location_id),
                timezone=charger.timezone,
            )
        )
    nodes.append(TripNode("destination", "Destination", "destination", destination, 1.0))
    if len(distances_miles) != len(nodes) or len(durations_minutes) != len(nodes):
        raise ValueError("Routing matrix size must match graph nodes")
    return GraphContext(
        nodes=nodes,
        distances_miles=distances_miles,
        durations_minutes=durations_minutes,
        base_distance_miles=base_distance_miles,
        base_duration_minutes=base_duration_minutes,
    )
