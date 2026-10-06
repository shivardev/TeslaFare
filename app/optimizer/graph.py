from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models import Charger, Coordinate, PricingSchedule


@dataclass(frozen=True)
class GraphWaypoint:
    """A user stop the plan must visit, in order, before continuing."""
    name: str
    coordinate: Coordinate
    dwell_minutes: float = 0.0


@dataclass(frozen=True)
class TripNode:
    id: str
    name: str
    kind: Literal["origin", "charger", "waypoint", "destination"]
    coordinate: Coordinate
    progress: float
    charger: Charger | None = None
    pricing: PricingSchedule | None = None
    timezone: str | None = None
    # Chargers belong to the leg they sit on; a waypoint/destination belongs to the leg it ends.
    leg: int = 0
    waypoint_index: int | None = None
    dwell_minutes: float = 0.0

    @property
    def departing_leg(self) -> int:
        """The leg the car drives on after leaving this node."""
        return self.leg + 1 if self.kind == "waypoint" else self.leg


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


LayoutEntry = tuple[Literal["origin", "charger", "waypoint", "destination"], int, Charger | None]


def graph_layout(chargers: list[Charger], leg_count: int) -> list[LayoutEntry]:
    """Node order shared by the routing matrix and the graph: origin, then per leg its chargers
    (by progress) followed by the stop that ends the leg. The last leg ends at the destination."""
    layout: list[LayoutEntry] = [("origin", 0, None)]
    for leg in range(leg_count):
        on_leg = sorted((c for c in chargers if c.route_leg == leg), key=lambda c: c.route_progress)
        layout.extend(("charger", leg, c) for c in on_leg)
        layout.append(("waypoint" if leg < leg_count - 1 else "destination", leg, None))
    return layout


def build_graph(
    origin: Coordinate,
    destination: Coordinate,
    chargers: list[Charger],
    pricing: dict[str, PricingSchedule],
    distances_miles: list[list[float | None]],
    durations_minutes: list[list[float | None]],
    base_distance_miles: float,
    base_duration_minutes: float,
    waypoints: list[GraphWaypoint] | None = None,
) -> GraphContext:
    waypoints = waypoints or []
    leg_count = len(waypoints) + 1
    nodes: list[TripNode] = []
    for kind, leg, charger in graph_layout(chargers, leg_count):
        if kind == "origin":
            nodes.append(TripNode("origin", "Origin", "origin", origin, 0.0))
        elif kind == "charger":
            assert charger is not None
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
                    leg=leg,
                )
            )
        elif kind == "waypoint":
            wp = waypoints[leg]
            nodes.append(
                TripNode(
                    id=f"waypoint-{leg}",
                    name=wp.name,
                    kind="waypoint",
                    coordinate=wp.coordinate,
                    progress=float(leg + 1),
                    leg=leg,
                    waypoint_index=leg,
                    dwell_minutes=wp.dwell_minutes,
                )
            )
        else:
            nodes.append(TripNode("destination", "Destination", "destination", destination, float(leg_count), leg=leg))
    if len(distances_miles) != len(nodes) or len(durations_minutes) != len(nodes):
        raise ValueError("Routing matrix size must match graph nodes")
    return GraphContext(
        nodes=nodes,
        distances_miles=distances_miles,
        durations_minutes=durations_minutes,
        base_distance_miles=base_distance_miles,
        base_duration_minutes=base_duration_minutes,
    )
