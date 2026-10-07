"""Slow background refresh of saved Supercharger prices.

Looks up one station at a time, well under Tesla's tolerance: stations on recently planned routes
first, then stations with no price, then the oldest prices. It shares the provider's hourly request
cap and always leaves headroom for visitors' own trips; it pauses entirely while Tesla is blocking.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.chargers.knowledge_store import ChargerKnowledgeStore
from app.models import Charger, PricingSchedule
from app.pricing.tesla import LiveLookupUnavailable, TeslaPriceProvider

log = logging.getLogger("teslafare")

USABLE_KINDS = {"flat", "time_of_use"}
RECENT_TRIP_STATIONS = 1000
# A station the refresher failed on is left alone this long, so one bad page can't eat the budget.
FAILED_RETRY_AFTER = timedelta(days=1)


def _age_key(schedule: PricingSchedule | None) -> datetime:
    fetched = schedule.fetched_at if schedule else None
    if fetched is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return fetched if fetched.tzinfo else fetched.replace(tzinfo=timezone.utc)


class PriceRefresher:
    def __init__(
        self,
        provider: TeslaPriceProvider,
        knowledge: ChargerKnowledgeStore,
        load_catalog: Callable[[], Awaitable[list[Charger]]],
        *,
        per_hour: int,
        stale_after: timedelta,
        countries: set[str],
        visitor_reserve: int,
    ):
        self.provider = provider
        self.knowledge = knowledge
        self.load_catalog = load_catalog
        self.per_hour = per_hour
        self.stale_after = stale_after
        self.countries = countries
        self.visitor_reserve = visitor_reserve
        self._recent_trip_ids: OrderedDict[str, None] = OrderedDict()

    def note_trip_stations(self, station_ids: list[str]) -> None:
        """Stations that appeared in a planned trip get refreshed first."""
        for station_id in station_ids:
            self._recent_trip_ids.pop(station_id, None)
            self._recent_trip_ids[station_id] = None
        while len(self._recent_trip_ids) > RECENT_TRIP_STATIONS:
            self._recent_trip_ids.popitem(last=False)

    def recent_trip_ids(self) -> list[str]:
        """Stations from recently planned trips, most recent first."""
        return list(reversed(self._recent_trip_ids))

    def next_station(self, catalog: list[Charger], saved: dict[str, PricingSchedule], now: datetime) -> Charger | None:
        recency = {sid: rank for rank, sid in enumerate(reversed(self._recent_trip_ids))}
        best: tuple | None = None
        best_station: Charger | None = None
        for charger in catalog:
            if self.countries and charger.country not in self.countries:
                continue
            schedule = saved.get(charger.location_id)
            usable = schedule is not None and schedule.kind in USABLE_KINDS
            age = _age_key(schedule)
            if usable and now - age < self.stale_after:
                continue
            if not usable and schedule is not None and now - age < FAILED_RETRY_AFTER:
                continue
            key = (recency.get(charger.location_id, len(recency)), usable, age)
            if best is None or key < best:
                best, best_station = key, charger
        return best_station

    def has_headroom(self) -> bool:
        return (
            not self.provider.blocked
            and self.provider.requests_in_last_hour < self.provider.requests_per_hour - self.visitor_reserve
        )

    async def refresh_one(self, now: datetime | None = None) -> str | None:
        """Refresh the most useful stale station. Returns its id, or None if nothing was done."""
        if not self.has_headroom():
            return None
        catalog = await self.load_catalog()
        saved = self.knowledge.all_pricing()
        station = self.next_station(catalog, saved, now or datetime.now(timezone.utc))
        if station is None:
            return None
        self.provider.reset_browser_budget()
        try:
            schedule = await self.provider.get_prices(station, force_refresh=True)
        except LiveLookupUnavailable:
            return None
        previous = saved.get(station.location_id)
        if schedule.kind not in USABLE_KINDS and previous is not None and previous.kind in USABLE_KINDS:
            log.info("Background refresh of %s failed; keeping the saved price", station.name)
            return station.location_id
        self.knowledge.remember_pricing(station, schedule)
        log.info("Background refresh: %s -> %s", station.name, schedule.kind)
        return station.location_id

    async def run(self) -> None:
        interval = 3600 / max(1, self.per_hour)
        log.info("Background price refresh on: up to %d stations/hour", self.per_hour)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad station stop the loop
                log.warning("Background price refresh failed: %s", exc)
