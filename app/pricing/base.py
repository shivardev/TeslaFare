from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import Charger, PricingSchedule


def charging_cost(kwh: float, price_per_kwh: float) -> float:
    return max(0.0, kwh) * max(0.0, price_per_kwh)


class PriceProvider(ABC):
    @abstractmethod
    async def get_prices(self, station: Charger) -> PricingSchedule:
        raise NotImplementedError


def price_for_time(schedule: PricingSchedule, when: datetime, station_timezone: str | None = None) -> float | None:
    if schedule.kind not in {"flat", "time_of_use", "estimate"} or not schedule.bands:
        return None
    local = when
    if station_timezone:
        local = when.astimezone(ZoneInfo(station_timezone))
    minute = local.hour * 60 + local.minute
    for band in schedule.bands:
        tesla_weekday = local.isoweekday() % 7
        if band.days and tesla_weekday not in band.days:
            continue
        start, end = band.start_minute, band.end_minute
        if start == end:
            return band.price_per_kwh
        if start < end and start <= minute < end:
            return band.price_per_kwh
        if start > end and (minute >= start or minute < end):
            return band.price_per_kwh
    return None
