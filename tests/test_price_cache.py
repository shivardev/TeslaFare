import asyncio

import app.main as main
from app.models import Charger, Coordinate, PriceBand, PricingSchedule

STATION = Charger(id="s1", location_id="s1", name="Normal, IL", coordinate=Coordinate(lat=0, lon=0), tesla_url="x")
FLAT = PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.39)])
FAILED = PricingSchedule(kind="unknown", note="Pricing fetch failed")


class FakeKnowledge:
    def __init__(self, saved):
        self.saved, self.remembered = saved, []

    def pricing(self, location_id):
        return self.saved

    def remember_pricing(self, charger, schedule):
        self.remembered.append(schedule)


class FakeProvider:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def reset_browser_budget(self):
        pass

    async def get_prices(self, charger, force_refresh=False):
        self.calls += 1
        return self.result


def run(monkeypatch, saved, live, use_cache=True):
    knowledge, provider = FakeKnowledge(saved), FakeProvider(live)
    monkeypatch.setattr(main, "charger_knowledge", knowledge)
    monkeypatch.setattr(main, "price_provider", provider)
    pricing = asyncio.run(main._fetch_prices([STATION], use_cache))
    return pricing["s1"], provider.calls, knowledge.remembered


def test_saved_price_is_reused_without_fetching(monkeypatch):
    schedule, calls, remembered = run(monkeypatch, saved=FLAT, live=FAILED)
    assert schedule.kind == "flat" and calls == 0 and remembered == []


def test_saved_failure_is_retried_instead_of_reused(monkeypatch):
    schedule, calls, remembered = run(monkeypatch, saved=FAILED, live=FLAT)
    assert calls == 1 and schedule.kind == "flat" and remembered[0].kind == "flat"


def test_failed_refresh_keeps_the_known_price(monkeypatch):
    schedule, calls, remembered = run(monkeypatch, saved=FLAT, live=FAILED, use_cache=False)
    assert calls == 1 and schedule.kind == "flat" and remembered == []
