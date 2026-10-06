"""Why a plan did or didn't use each candidate charger.

For a chosen plan we know when the car leaves each plan node (origin, charging stop, user stop)
and with how much battery. For every other charger in the graph we find the plan leg it sits on and
estimate when the car would pass it, with what battery, at what price — then compare with what the
plan actually does nearby.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from app.models import ChargingStop, TripPlan
from app.optimizer.graph import GraphContext
from app.optimizer.search import OptimizerConfig
from app.pricing.base import price_for_time
from app.vehicle.energy import EnergyModel

Verdict = Literal["used", "out_of_range", "too_early", "pricier", "not_needed", "detour", "no_saving", "off_path"]

# A detour at least this long is called out as the likely reason a similarly priced charger lost.
DETOUR_NOTE_MINUTES = 5.0
PRICE_EPSILON = 0.005
# Passing with at least this much battery, stopping isn't the question yet; say so before comparing prices.
TOO_EARLY_SOC = 50.0


class ChargerExplanation(BaseModel):
    station_id: str
    verdict: Verdict
    pass_time: datetime | None = None
    pass_soc: float | None = None
    price_at_pass: float | None = None
    detour_minutes: float | None = None
    # The plan's cheapest charging stop (where it buys the most energy on ties): the price to beat.
    ref_station_name: str | None = None
    ref_price: float | None = None
    # For time-of-use stations: when the price next drops below price_at_pass.
    cheaper_at: datetime | None = None
    cheaper_price: float | None = None
    # Road miles driven since the last charge (or the start) when reaching this charger, and trip miles so far.
    miles_from_last_charge: float | None = None
    last_charge_name: str | None = None
    trip_miles: float | None = None


@dataclass(frozen=True)
class _Event:
    node_idx: int
    depart_time: datetime
    depart_soc: float
    stop: ChargingStop | None = None


def _plan_events(graph: GraphContext, plan: TripPlan, starting_soc: float) -> list[_Event] | None:
    """Plan nodes in driving order, mapped onto graph node indices."""
    nodes = graph.nodes
    visits: list[tuple[datetime, int, object]] = [(s.arrival_time, 0, s) for s in plan.stops]
    visits += [(w.arrival_time, 1, w) for w in plan.waypoints]
    visits.sort(key=lambda v: (v[0], v[1]))
    events = [_Event(0, plan.departure_time, plan.starting_soc if plan.starting_soc is not None else starting_soc)]
    leg = 0
    for _, kind, item in visits:
        if kind == 0:
            stop: ChargingStop = item  # type: ignore[assignment]
            candidates = [i for i, n in enumerate(nodes) if n.kind == "charger" and n.id == stop.station_id]
            same_leg = [i for i in candidates if nodes[i].leg == leg]
            if not candidates:
                return None
            idx = (same_leg or candidates)[0]
            events.append(_Event(idx, stop.arrival_time + timedelta(minutes=stop.charging_minutes), stop.departure_soc, stop))
        else:
            idx = next((i for i, n in enumerate(nodes) if n.kind == "waypoint" and n.waypoint_index == item.index), None)
            if idx is None:
                return None
            events.append(_Event(idx, item.departure_time, item.arrival_soc))
            leg = item.index + 1
    events.append(_Event(graph.destination_index, plan.arrival_time, plan.arrival_soc or 0.0))
    return events


def _next_cheaper(node, when: datetime, price: float) -> tuple[datetime | None, float | None]:
    if node.pricing is None or node.pricing.kind != "time_of_use":
        return None, None
    start = when.replace(second=0, microsecond=0)
    for step in range(1, 12 * 60 + 1):  # look ahead 12 hours, minute by minute, to land on the band edge
        t = start + timedelta(minutes=step)
        p = price_for_time(node.pricing, t, node.timezone)
        if p is not None and p < price - PRICE_EPSILON:
            return t, p
    return None, None


def explain_plan(graph: GraphContext, plan: TripPlan, energy: EnergyModel, cfg: OptimizerConfig) -> dict[str, ChargerExplanation]:
    events = _plan_events(graph, plan, cfg.starting_soc)
    if events is None:
        return {}
    used = {e.stop.station_id for e in events if e.stop}
    # Cumulative road miles at each plan node, and the most recent charge point (or start) at or before it.
    miles_at = [0.0]
    for a, b in zip(events, events[1:]):
        miles_at.append(miles_at[-1] + (graph.distances_miles[a.node_idx][b.node_idx] or 0.0))
    last_charge = []
    for i, e in enumerate(events):
        last_charge.append(i if i == 0 or e.stop else last_charge[-1])

    def charge_name(i: int) -> str:
        return events[i].stop.station_name if events[i].stop else "Start"

    result: dict[str, ChargerExplanation] = {}
    for c_idx, node in enumerate(graph.nodes):
        if node.kind != "charger":
            continue
        sid = node.id
        if sid in used:
            j = next(i for i, e in enumerate(events) if e.stop and e.stop.station_id == sid)
            k = last_charge[j - 1]
            result[sid] = ChargerExplanation(
                station_id=sid, verdict="used", miles_from_last_charge=round(miles_at[j] - miles_at[k], 1),
                last_charge_name=charge_name(k), trip_miles=round(miles_at[j], 1),
            )
            continue
        # The plan segment whose leg and progress range contain this charger.
        seg = None
        for i in range(len(events) - 1):
            a, b = graph.nodes[events[i].node_idx], graph.nodes[events[i + 1].node_idx]
            if a.departing_leg == node.leg and a.progress - 1e-9 <= node.progress <= b.progress + 1e-9:
                seg = i
                break
        if seg is None:
            result.setdefault(sid, ChargerExplanation(station_id=sid, verdict="off_path"))
            continue
        start, end = events[seg], events[seg + 1]
        dist = graph.distances_miles[start.node_idx][c_idx]
        t_in = graph.durations_minutes[start.node_idx][c_idx]
        t_out = graph.durations_minutes[c_idx][end.node_idx]
        t_direct = graph.durations_minutes[start.node_idx][end.node_idx]
        if dist is None or t_in is None:
            result.setdefault(sid, ChargerExplanation(station_id=sid, verdict="off_path"))
            continue
        pass_time = start.depart_time + timedelta(minutes=t_in)
        pass_soc = start.depart_soc - energy.soc_points(dist)
        if start.stop is not None:
            # Right after a plan charge, the real alternative is going here *instead of* that charge.
            instead_soc = start.stop.arrival_soc - energy.soc_points(dist)
            if instead_soc >= cfg.min_charger_soc - 0.5:
                pass_time = start.stop.arrival_time + timedelta(minutes=t_in)
                pass_soc = instead_soc
        detour = None if t_out is None or t_direct is None else max(0.0, t_in + t_out - t_direct)
        price = price_for_time(node.pricing, pass_time, node.timezone) if node.pricing else None
        ref = min(plan.stops, key=lambda s: (s.price_per_kwh, -s.kwh_purchased), default=None)
        explanation = ChargerExplanation(
            station_id=sid,
            verdict="no_saving",
            pass_time=pass_time,
            pass_soc=round(pass_soc, 1),
            price_at_pass=price,
            detour_minutes=None if detour is None else round(detour, 1),
            ref_station_name=ref.station_name if ref else None,
            ref_price=ref.price_per_kwh if ref else None,
            miles_from_last_charge=round(miles_at[seg] - miles_at[last_charge[seg]] + dist, 1),
            last_charge_name=charge_name(last_charge[seg]),
            trip_miles=round(miles_at[seg] + dist, 1),
        )
        if pass_soc < cfg.min_charger_soc - 0.5:
            explanation.verdict = "out_of_range"
        elif ref is None:
            explanation.verdict = "not_needed"
        elif pass_soc >= TOO_EARLY_SOC:
            explanation.verdict = "too_early"
        elif price is not None and price > ref.price_per_kwh + PRICE_EPSILON:
            explanation.verdict = "pricier"
            explanation.cheaper_at, explanation.cheaper_price = _next_cheaper(node, pass_time, price)
        elif detour is not None and detour >= DETOUR_NOTE_MINUTES:
            explanation.verdict = "detour"
        # A station on several legs: keep the first explanation (earliest pass) unless it was off-path.
        current = result.get(sid)
        if current is None or current.verdict == "off_path":
            result[sid] = explanation
    return result
