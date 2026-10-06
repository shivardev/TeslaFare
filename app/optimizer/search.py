from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import ChargingStop, TripPlan, WaypointVisit
from app.optimizer.graph import GraphContext, TripNode
from app.pricing.base import charging_cost, price_for_time
from app.vehicle.charging import ChargingModel
from app.vehicle.energy import EnergyModel


@dataclass(frozen=True)
class OptimizerConfig:
    starting_soc: float
    min_charger_soc: float
    destination_soc: float
    max_preferred_charge_soc: float
    absolute_max_charge_soc: float
    max_route_detour_percent: float
    max_total_extra_driving_minutes: float
    max_states: int = 60_000
    results_per_departure: int = 3


@dataclass(order=True)
class _QueueItem:
    priority: float
    seq: int
    state: "_State" = field(compare=False)


@dataclass(frozen=True)
class _State:
    node_idx: int
    soc: float
    timestamp: datetime
    cost: float
    driving_minutes: float
    charging_minutes: float
    miles: float
    kwh_purchased: float
    stops: tuple[ChargingStop, ...]
    visited: tuple[int, ...]
    dwell_minutes: float = 0.0
    waypoints: tuple[WaypointVisit, ...] = ()
    required_done: bool = False


def _ceil_half(value: float) -> float:
    return math.ceil(value * 2.0 - 1e-9) / 2.0


def _arrival_reserve(node: TripNode, cfg: OptimizerConfig) -> float:
    return cfg.min_charger_soc if node.kind == "charger" else cfg.destination_soc


def _can_drive(node: TripNode, nxt: TripNode) -> bool:
    """Fixed stop order: from any node the car may only reach nodes on the leg it is driving,
    moving forward, and every leg must end at its own stop (no skipping, no reordering)."""
    if nxt.kind == "origin" or nxt.leg != node.departing_leg:
        return False
    if nxt.kind in {"waypoint", "destination"}:
        return True
    # Keep the search moving toward the leg end; tiny tolerance handles clustered sites.
    return nxt.progress > node.progress + 0.002


def _charge_targets(
    state: _State,
    node: TripNode,
    graph: GraphContext,
    energy: EnergyModel,
    cfg: OptimizerConfig,
) -> list[float]:
    if node.kind != "charger":
        return [state.soc]

    targets: set[float] = {round(state.soc, 2)}
    for target in (50.0, 60.0, 70.0, 80.0, cfg.max_preferred_charge_soc):
        if state.soc < target <= cfg.max_preferred_charge_soc:
            targets.add(target)

    # Intelligent targets: enough energy to reach any useful downstream node + reserve.
    for j, nxt in enumerate(graph.nodes):
        if j == state.node_idx or nxt.progress <= node.progress + 1e-5:
            continue
        distance = graph.distances_miles[state.node_idx][j]
        if distance is None:
            continue
        required = energy.soc_points(distance) + _arrival_reserve(nxt, cfg)
        if required <= cfg.absolute_max_charge_soc + 1e-9 and required > state.soc:
            targets.add(_ceil_half(required))

    return sorted(t for t in targets if state.soc - 1e-6 <= t <= cfg.absolute_max_charge_soc + 1e-6)


def _state_key(state: _State, departure: datetime) -> tuple[int, int, int]:
    soc_bucket = int(round(state.soc / 2.5))
    elapsed = max(0.0, (state.timestamp - departure).total_seconds() / 60.0)
    time_bucket = int(elapsed // 15.0)
    return state.node_idx, soc_bucket, time_bucket


def optimize_departure(
    graph: GraphContext,
    departure: datetime,
    energy: EnergyModel,
    charging: ChargingModel,
    cfg: OptimizerConfig,
    required_station_id: str | None = None,
) -> tuple[list[TripPlan], int]:
    """Cheapest plans for one departure. With `required_station_id`, only plans that charge at
    that station are returned (used for "what if I charge here?" comparisons)."""
    required_nodes = {
        i for i, n in enumerate(graph.nodes) if required_station_id and n.kind == "charger" and n.id == required_station_id
    }
    if required_station_id and not required_nodes:
        return [], 0
    # Past this point on the route the required station can no longer be visited.
    required_last_progress = max((graph.nodes[i].progress for i in required_nodes), default=0.0)
    known_prices = [
        band.price_per_kwh
        for node in graph.nodes
        if node.pricing is not None
        for band in node.pricing.bands
    ]
    min_network_price = min(known_prices, default=0.0)

    def remaining_cost_floor(node_idx: int, soc: float) -> float:
        """Admissible A* floor based on direct road energy at the cheapest rate."""
        remaining_miles = graph.distances_miles[node_idx][graph.destination_index]
        if remaining_miles is None:
            return 0.0
        needed_soc = energy.soc_points(remaining_miles) + cfg.destination_soc
        missing_soc = max(0.0, needed_soc - soc)
        return missing_soc / 100.0 * energy.battery_usable_kwh * min_network_price

    initial = _State(
        node_idx=0,
        soc=cfg.starting_soc,
        timestamp=departure,
        cost=0.0,
        driving_minutes=0.0,
        charging_minutes=0.0,
        miles=0.0,
        kwh_purchased=0.0,
        stops=(),
        visited=(0,),
    )
    seq = itertools.count()
    heap: list[_QueueItem] = [_QueueItem(0.0, next(seq), initial)]
    # Keep a few labels per bucket to preserve useful route/time alternatives.
    labels: dict[tuple[int, int, int, bool], list[tuple[float, float]]] = {}
    destination_states: list[_State] = []
    destination_signatures: set[tuple[str, ...]] = set()
    evaluated = 0

    max_miles = graph.base_distance_miles * (1.0 + cfg.max_route_detour_percent / 100.0)
    max_drive_minutes = graph.base_duration_minutes + cfg.max_total_extra_driving_minutes

    while heap and evaluated < cfg.max_states:
        item = heapq.heappop(heap)
        state = item.state
        evaluated += 1
        node = graph.nodes[state.node_idx]

        if state.node_idx == graph.destination_index:
            if required_nodes and not state.required_done:
                continue
            sig = tuple(stop.station_id for stop in state.stops)
            if sig not in destination_signatures:
                destination_signatures.add(sig)
                destination_states.append(state)
            if len(destination_states) >= max(cfg.results_per_departure * 3, 6):
                # Dijkstra order means later states are generally more expensive; enough variety for MVP.
                break
            continue

        for target_soc in _charge_targets(state, node, graph, energy, cfg):
            extra_cost = 0.0
            charge_minutes = 0.0
            kwh = 0.0
            new_stops = state.stops
            depart_time = state.timestamp
            required_done = state.required_done

            if target_soc > state.soc + 0.05:
                if node.kind != "charger" or node.pricing is None:
                    continue
                price = price_for_time(node.pricing, state.timestamp, node.timezone)
                if price is None:
                    continue
                kwh = charging.kwh_between(state.soc, target_soc)
                charge_minutes = charging.minutes_between(state.soc, target_soc)
                extra_cost = charging_cost(kwh, price)
                depart_time = state.timestamp + timedelta(minutes=charge_minutes)
                assert node.charger is not None
                new_stops = state.stops + (
                    ChargingStop(
                        station_id=node.id,
                        station_name=node.name,
                        tesla_url=node.charger.tesla_url,
                        arrival_time=state.timestamp,
                        arrival_soc=round(state.soc, 1),
                        price_per_kwh=round(price, 4),
                        price_is_estimate=node.pricing.kind == "estimate",
                        kwh_purchased=round(kwh, 2),
                        departure_soc=round(target_soc, 1),
                        charging_minutes=round(charge_minutes, 1),
                        cost=round(extra_cost, 2),
                        coordinate=node.coordinate,
                    ),
                )
                required_done = required_done or state.node_idx in required_nodes

            for nxt_idx, nxt in enumerate(graph.nodes):
                if nxt_idx == state.node_idx or nxt_idx in state.visited:
                    continue
                if not _can_drive(node, nxt):
                    continue
                if required_nodes and not required_done and nxt.progress > required_last_progress + 1e-9:
                    continue
                distance = graph.distances_miles[state.node_idx][nxt_idx]
                duration = graph.durations_minutes[state.node_idx][nxt_idx]
                if distance is None or duration is None:
                    continue
                reserve = _arrival_reserve(nxt, cfg)
                consumed_soc = energy.soc_points(distance)
                arrival_soc = target_soc - consumed_soc
                if arrival_soc < reserve - 1e-6:
                    continue

                new_miles = state.miles + distance
                new_drive = state.driving_minutes + duration
                if new_miles > max_miles + 1e-6 or new_drive > max_drive_minutes + 1e-6:
                    continue

                arrival_time = depart_time + timedelta(minutes=duration)
                dwell = 0.0
                new_waypoints = state.waypoints
                if nxt.kind == "waypoint":
                    dwell = nxt.dwell_minutes
                    new_waypoints = state.waypoints + (
                        WaypointVisit(
                            index=nxt.waypoint_index or 0,
                            name=nxt.name,
                            coordinate=nxt.coordinate,
                            arrival_time=arrival_time,
                            arrival_soc=round(arrival_soc, 1),
                            dwell_minutes=dwell,
                            departure_time=arrival_time + timedelta(minutes=dwell),
                        ),
                    )
                nxt_state = _State(
                    node_idx=nxt_idx,
                    soc=arrival_soc,
                    timestamp=arrival_time + timedelta(minutes=dwell),
                    cost=state.cost + extra_cost,
                    driving_minutes=new_drive,
                    charging_minutes=state.charging_minutes + charge_minutes,
                    miles=new_miles,
                    kwh_purchased=state.kwh_purchased + kwh,
                    stops=new_stops,
                    visited=state.visited + (nxt_idx,),
                    dwell_minutes=state.dwell_minutes + dwell,
                    waypoints=new_waypoints,
                    required_done=required_done,
                )
                key = _state_key(nxt_state, departure) + (required_done,)
                metric = (round(nxt_state.cost, 5), round(nxt_state.driving_minutes + nxt_state.charging_minutes, 2))
                bucket = labels.setdefault(key, [])
                dominated = any(c <= metric[0] + 1e-6 and t <= metric[1] + 0.5 for c, t in bucket)
                if dominated:
                    continue
                bucket.append(metric)
                bucket.sort()
                del bucket[3:]
                # Primary objective dollars, tiny time tie-breaker only.
                if len(graph.nodes) > 12:
                    # On a long real-world corridor, force the bounded search to
                    # complete forward-moving route alternatives before expanding
                    # thousands of partial charge variants. Exact dollars still
                    # rank the completed plans across stations and departures.
                    priority = -nxt.progress * 10_000.0 + nxt_state.cost
                else:
                    priority = (
                        nxt_state.cost
                        + remaining_cost_floor(nxt_idx, arrival_soc)
                        + (nxt_state.driving_minutes + nxt_state.charging_minutes) * 1e-6
                    )
                heapq.heappush(heap, _QueueItem(priority, next(seq), nxt_state))

    plans = []
    for state in destination_states:
        plans.append(
            TripPlan(
                departure_time=departure,
                arrival_time=state.timestamp,
                total_miles=round(state.miles, 1),
                driving_minutes=round(state.driving_minutes, 1),
                charging_minutes=round(state.charging_minutes, 1),
                dwell_minutes=round(state.dwell_minutes, 1),
                total_minutes=round(state.driving_minutes + state.charging_minutes + state.dwell_minutes, 1),
                charging_cost=round(state.cost, 2),
                kwh_purchased=round(state.kwh_purchased, 2),
                starting_soc=round(cfg.starting_soc, 1),
                arrival_soc=round(state.soc, 1),
                stops=list(state.stops),
                waypoints=list(state.waypoints),
            )
        )
    plans.sort(key=lambda p: (p.charging_cost, p.total_minutes))
    return plans[: cfg.results_per_departure], evaluated


def choose_useful_plans(plans: list[TripPlan], base_distance_miles: float) -> list[TripPlan]:
    if not plans:
        return []
    # Deduplicate exact same departure+stop sequence+cost.
    dedup: dict[tuple, TripPlan] = {}
    for p in plans:
        sig = (
            p.departure_time.isoformat(),
            tuple(s.station_id for s in p.stops),
            round(p.charging_cost, 2),
        )
        dedup[sig] = p
    pool = list(dedup.values())
    for p in pool:
        p.extra_miles_vs_fastest = round(max(0.0, p.total_miles - base_distance_miles), 1)

    cheapest = min(pool, key=lambda p: (p.charging_cost, p.total_minutes))
    fastest = min(pool, key=lambda p: (p.total_minutes, p.charging_cost))
    expensive = max(pool, key=lambda p: (p.charging_cost, -p.total_minutes))

    near_cheap = [p for p in pool if p.charging_cost <= cheapest.charging_cost * 1.12 + 0.75]
    cheap_fast = min(near_cheap, key=lambda p: (p.total_minutes, p.charging_cost))

    min_cost, max_cost = min(p.charging_cost for p in pool), max(p.charging_cost for p in pool)
    min_time, max_time = min(p.total_minutes for p in pool), max(p.total_minutes for p in pool)
    cost_span = max(0.01, max_cost - min_cost)
    time_span = max(0.01, max_time - min_time)
    balanced = min(
        pool,
        key=lambda p: 0.58 * ((p.charging_cost - min_cost) / cost_span)
        + 0.42 * ((p.total_minutes - min_time) / time_span),
    )

    ordered = [
        ("CHEAPEST", cheapest),
        ("CHEAP + FAST", cheap_fast),
        ("BALANCED", balanced),
        ("FASTEST REASONABLE", fastest),
        ("MOST EXPENSIVE REASONABLE", expensive),
    ]
    used: set[tuple] = set()
    result: list[TripPlan] = []
    for category, p in ordered:
        sig = (p.departure_time.isoformat(), tuple(s.station_id for s in p.stops), p.charging_cost)
        if sig in used:
            continue
        used.add(sig)
        q = p.model_copy(deep=True)
        q.category = category
        result.append(q)
    return result
