"""Prices captured by the host's own browser (userscript) instead of server-side lookups.

The userscript sends the ``get-charger-details`` JSON that Tesla's Find Us page already loaded while
the host was looking at a station. This module works out which station it belongs to and ranks the
stations that still need a price for the collection queue page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

from app.models import Charger, PricingSchedule

USABLE_KINDS = {"flat", "time_of_use"}


def findus_url(location_id: str) -> str:
    return f"https://www.tesla.com/findus?location={location_id}"


def _strings(value: Any, limit: int = 2000):
    """Every string inside a JSON value (bounded), used to spot a station id in the payload."""
    stack, seen = [value], 0
    while stack and seen < limit:
        item = stack.pop()
        seen += 1
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def identify_station(page_url: str, request_url: str, payload: Any, catalog: dict[str, Charger]) -> Charger | None:
    """The station a captured payload belongs to: the page's ?location=, then the details request's
    query values, then any matching id inside the payload itself."""
    for url in (page_url, request_url):
        for _, value in parse_qsl(urlparse(url or "").query):
            if value in catalog:
                return catalog[value]
    for text in _strings(payload):
        if text in catalog:
            return catalog[text]
    return None


def price_age_hours(schedule: PricingSchedule | None, now: datetime) -> float | None:
    if schedule is None or schedule.fetched_at is None:
        return None
    fetched = schedule.fetched_at if schedule.fetched_at.tzinfo else schedule.fetched_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - fetched).total_seconds() / 3600)


def collection_queue(
    catalog: list[Charger],
    saved: dict[str, PricingSchedule],
    recent_trip_ids: list[str],
    now: datetime,
    fresh_for: timedelta,
    countries: set[str],
) -> tuple[list[dict], dict[str, int]]:
    """Stations needing a price, best first: on recently planned routes, then never priced (or failed),
    then the oldest prices. Fresh prices are left out. Also returns fresh/stale/missing counts."""
    route_rank = {sid: i for i, sid in enumerate(recent_trip_ids)}
    counts = {"total": 0, "fresh": 0, "stale": 0, "missing": 0}
    items = []
    for charger in catalog:
        if countries and charger.country not in countries:
            continue
        counts["total"] += 1
        schedule = saved.get(charger.location_id)
        usable = schedule is not None and schedule.kind in USABLE_KINDS
        age = price_age_hours(schedule, now) if usable else None
        if usable and age is not None and age < fresh_for.total_seconds() / 3600:
            counts["fresh"] += 1
            continue
        status = "stale" if usable else ("failed" if schedule is not None else "missing")
        counts["stale" if usable else "missing"] += 1
        items.append({
            "station_id": charger.location_id,
            "name": charger.name,
            "address": charger.address,
            "status": status,
            "age_hours": None if age is None else round(age, 1),
            "on_recent_route": charger.location_id in route_rank,
            "tesla_url": findus_url(charger.location_id),
            "_key": (route_rank.get(charger.location_id, len(route_rank)), usable, -(age or 1e9)),
        })
    items.sort(key=lambda item: item["_key"])
    for item in items:
        del item["_key"]
    return items, counts
