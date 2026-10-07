import asyncio
from datetime import datetime, timedelta, timezone

from app.models import Charger, Coordinate, PriceBand, PricingSchedule
from app.pricing.refresher import PriceRefresher

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def station(sid, country="USA"):
    return Charger(id=sid, location_id=sid, name=sid, coordinate=Coordinate(lat=0, lon=0), country=country, tesla_url="x")


def price(age_days, kind="flat"):
    bands = [PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.4)] if kind != "unknown" else []
    return PricingSchedule(kind=kind, bands=bands, fetched_at=NOW - timedelta(days=age_days))


class FakeProvider:
    def __init__(self, result=None, used=0, blocked=False):
        self.result, self.calls = result, []
        self.requests_in_last_hour, self.requests_per_hour, self.blocked = used, 30, blocked

    def reset_browser_budget(self):
        pass

    async def get_prices(self, charger, force_refresh=False):
        self.calls.append(charger.location_id)
        return self.result


class FakeKnowledge:
    def __init__(self, saved):
        self.saved, self.remembered = saved, {}

    def all_pricing(self):
        return dict(self.saved)

    def remember_pricing(self, charger, schedule):
        self.remembered[charger.location_id] = schedule


def refresher(provider, knowledge, catalog):
    async def load():
        return catalog
    return PriceRefresher(provider, knowledge, load, per_hour=20, stale_after=timedelta(days=7),
                          countries={"USA"}, visitor_reserve=10)


def test_priority_trip_stations_then_missing_prices_then_oldest():
    catalog = [station("old"), station("missing"), station("fresh"), station("on-route"), station("de", "Germany")]
    saved = {"old": price(20), "fresh": price(1), "on-route": price(9)}
    r = refresher(FakeProvider(), FakeKnowledge(saved), catalog)
    assert r.next_station(catalog, saved, NOW).location_id == "missing"
    r.note_trip_stations(["on-route"])
    assert r.next_station(catalog, saved, NOW).location_id == "on-route"
    saved["missing"] = price(0)
    r2 = refresher(FakeProvider(), FakeKnowledge(saved), catalog)
    assert r2.next_station(catalog, saved, NOW).location_id == "old"  # oldest stale price; fresh and Germany skipped


def test_recent_failures_are_left_alone_for_a_day():
    catalog = [station("failed")]
    r = refresher(FakeProvider(), FakeKnowledge({}), catalog)
    assert r.next_station(catalog, {"failed": price(0.1, "unknown")}, NOW) is None
    assert r.next_station(catalog, {"failed": price(2, "unknown")}, NOW).location_id == "failed"


def test_refresher_keeps_headroom_for_visitors_and_pauses_when_blocked():
    catalog = [station("a")]
    busy = FakeProvider(result=price(0), used=20)  # 30/hour cap minus 10 reserved for visitors
    assert asyncio.run(refresher(busy, FakeKnowledge({}), catalog).refresh_one(NOW)) is None and busy.calls == []
    blocked = FakeProvider(result=price(0), blocked=True)
    assert asyncio.run(refresher(blocked, FakeKnowledge({}), catalog).refresh_one(NOW)) is None and blocked.calls == []
    ok = FakeProvider(result=price(0))
    knowledge = FakeKnowledge({})
    assert asyncio.run(refresher(ok, knowledge, catalog).refresh_one(NOW)) == "a"
    assert knowledge.remembered["a"].kind == "flat"


def test_failed_refresh_keeps_the_saved_price():
    catalog = [station("a")]
    knowledge = FakeKnowledge({"a": price(30)})
    asyncio.run(refresher(FakeProvider(result=price(0, "unknown")), knowledge, catalog).refresh_one(NOW))
    assert knowledge.remembered == {}


def test_visitor_prices_are_validated_and_never_stored():
    import pytest
    from app.main import TripRequest
    req = TripRequest(from_location="A city", to_location="B city", price_overrides={"s1": 0.31})
    assert req.price_overrides == {"s1": 0.31}
    with pytest.raises(ValueError):
        TripRequest(from_location="A city", to_location="B city", price_overrides={"s1": 5})


def test_shared_manual_prices_are_off_by_default():
    import pytest
    from fastapi import HTTPException
    import app.main as main
    with pytest.raises(HTTPException) as err:
        asyncio.run(main.save_manual_price("s1", main.ManualPriceRequest(price_per_kwh=0.3)))
    assert err.value.status_code == 403
