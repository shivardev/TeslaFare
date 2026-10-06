from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.cache import CacheDB
from app.models import PriceBand, PricingSchedule
from app.pricing.base import charging_cost, price_for_time
from app.pricing.tesla import TeslaPriceProvider, parse_tesla_pricing_payload, parse_tesla_pricing_text


def test_price_schedule_selection():
    schedule = PricingSchedule(kind="time_of_use", bands=[
        PriceBand(start_minute=0, end_minute=8*60, price_per_kwh=0.22),
        PriceBand(start_minute=8*60, end_minute=21*60, price_per_kwh=0.42),
        PriceBand(start_minute=21*60, end_minute=0, price_per_kwh=0.23),
    ])
    assert price_for_time(schedule, datetime(2026,1,1,7,30,tzinfo=timezone.utc), "UTC") == 0.22
    assert price_for_time(schedule, datetime(2026,1,1,12,0,tzinfo=timezone.utc), "UTC") == 0.42
    assert price_for_time(schedule, datetime(2026,1,1,23,0,tzinfo=timezone.utc), "UTC") == 0.23


def test_charging_cost():
    assert charging_cost(22.5, 0.20) == 4.5


def test_parse_tesla_visible_text():
    text = """Charging Fees for Tesla Owner
    12:00 AM - 4:00 AM $0.23/kWh
    4:00 AM - 8:00 AM $0.23/kWh
    8:00 AM - 9:00 PM $0.43/kWh
    9:00 PM - 12:00 AM $0.23/kWh
    Charging Fees for All EVs 12:00 AM - 4:00 AM $0.33/kWh"""
    schedule = parse_tesla_pricing_text(text, "https://example.test")
    assert schedule.kind == "time_of_use"
    assert len(schedule.bands) == 4
    assert schedule.bands[2].price_per_kwh == 0.43


def test_parse_tesla_charger_details_pricebook():
    payload = {"data": {"data": {"timeZone": "America/Chicago", "effectivePricebooks": [
        {"feeType": "CONGESTION", "rateBase": 0.75, "uom": "min", "isTou": False, "vehicleMakeType": "TSLA"},
        {"feeType": "CHARGING", "rateBase": 0.49, "uom": "kwh", "startTime": "19:00", "endTime": "00:00", "isTou": True, "vehicleMakeType": "TSLA"},
        {"feeType": "CHARGING", "rateBase": 0.59, "uom": "kwh", "startTime": "09:00", "endTime": "19:00", "isTou": True, "vehicleMakeType": "TSLA"},
        {"feeType": "CHARGING", "rateBase": 0.45, "uom": "kwh", "startTime": "00:00", "endTime": "09:00", "isTou": True, "vehicleMakeType": "TSLA"},
        {"feeType": "CHARGING", "rateBase": 0.99, "uom": "kwh", "startTime": "00:00", "endTime": "09:00", "isTou": True, "vehicleMakeType": "NTSLA"},
    ]}}}
    schedule = parse_tesla_pricing_payload(payload, "https://www.tesla.com/findus")
    assert schedule.kind == "time_of_use"
    assert schedule.timezone == "America/Chicago"
    assert [(b.start_minute, b.end_minute, b.price_per_kwh) for b in schedule.bands] == [
        (19 * 60, 0, 0.49), (9 * 60, 19 * 60, 0.59), (0, 9 * 60, 0.45)
    ]


@pytest.mark.asyncio
async def test_playwright_fallback_disabled_when_subprocess_not_supported(monkeypatch):
    import playwright.sync_api as playwright_sync_api

    class BrokenPlaywright:
        def start(self):
            raise NotImplementedError("subprocess not supported")

    monkeypatch.setattr(playwright_sync_api, "sync_playwright", lambda: BrokenPlaywright())

    provider = TeslaPriceProvider(
        CacheDB(Path(".data/test-cache.sqlite3")), 8, "test-agent", 6, True, 3,
        False, "firefox", "playwright"
    )

    with pytest.raises(RuntimeError, match="Playwright fallback unavailable"):
        await provider._browser_text("https://www.tesla.com/findus/location/supercharger/32433")

    assert provider.use_playwright_fallback is False
    await provider.close()
