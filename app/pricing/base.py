from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import Charger, PriceBand, PricingSchedule


def charging_cost(kwh: float, price_per_kwh: float) -> float:
    return max(0.0, kwh) * max(0.0, price_per_kwh)


# Tesla per-minute tiers: <=60 kW, 60-100 kW, 100-180 kW, >180 kW.
MINUTE_TIER_LIMITS_KW = (60.0, 100.0, 180.0)


def minute_rate_for_power(rates: list[float], kw: float) -> float:
    tier = sum(1 for limit in MINUTE_TIER_LIMITS_KW if kw > limit)
    return rates[min(tier, len(rates) - 1)]


def session_cost(charging, start_soc: float, end_soc: float, band: PriceBand) -> float:
    """Cost of charging from start_soc to end_soc under one price band: per kWh, or per minute by power tier."""
    if band.minute_rates:
        return sum(minutes * minute_rate_for_power(band.minute_rates, kw) for minutes, kw in charging.segments(start_soc, end_soc))
    return charging_cost(charging.kwh_between(start_soc, end_soc), band.price_per_kwh)


class PriceProvider(ABC):
    @abstractmethod
    async def get_prices(self, station: Charger) -> PricingSchedule:
        raise NotImplementedError


def price_for_time(schedule: PricingSchedule, when: datetime, station_timezone: str | None = None) -> float | None:
    """$/kWh in effect (for per-minute sites, the band's reference estimate)."""
    band = band_for_time(schedule, when, station_timezone)
    return band.price_per_kwh if band else None


def band_for_time(schedule: PricingSchedule, when: datetime, station_timezone: str | None = None) -> PriceBand | None:
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
            return band
        if start < end and start <= minute < end:
            return band
        if start > end and (minute >= start or minute < end):
            return band
    return None
