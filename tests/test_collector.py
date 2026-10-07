import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app.main as main
from app.models import Charger, Coordinate, PriceBand, PricingSchedule
from app.pricing.collector import collection_queue, identify_station

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
LEX = Charger(id="lex", location_id="lexingtonkysupercharger", name="Lexington, KY", coordinate=Coordinate(lat=0, lon=0), country="USA")
# Shape of Tesla's get-charger-details response, as parsed by parse_tesla_pricing_payload.
PAYLOAD = {"data": {"effectivePricebooks": [
    {"feeType": "CHARGING", "uom": "kwh", "vehicleMakeType": "TSLA", "rateBase": 0.39, "isTou": False},
    {"feeType": "CHARGING", "uom": "kwh", "vehicleMakeType": "NTSLA", "rateBase": 0.52, "isTou": False},
]}}


def test_station_is_identified_from_page_url_request_url_or_payload():
    catalog = {LEX.location_id: LEX}
    assert identify_station("https://www.tesla.com/findus?location=lexingtonkysupercharger", "", {}, catalog) is LEX
    assert identify_station("https://www.tesla.com/findus", "https://x/get-charger-details?locationSlug=lexingtonkysupercharger", {}, catalog) is LEX
    assert identify_station("", "", {"data": {"slug": "lexingtonkysupercharger"}}, catalog) is LEX
    assert identify_station("https://www.tesla.com/findus", "", {}, catalog) is None


def price(hours_old):
    return PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.4)],
                           fetched_at=NOW - timedelta(hours=hours_old))


def test_queue_order_and_counts():
    station = lambda sid, country="USA": Charger(id=sid, location_id=sid, name=sid, coordinate=Coordinate(lat=0, lon=0), country=country)
    catalog = [station("fresh"), station("old"), station("older"), station("never"), station("route"), station("ca", "Canada")]
    saved = {"fresh": price(2), "old": price(30), "older": price(90), "route": price(40)}
    items, counts = collection_queue(catalog, saved, ["route"], NOW, timedelta(hours=24), {"USA"})
    assert [i["station_id"] for i in items] == ["route", "never", "older", "old"]
    assert counts == {"total": 5, "fresh": 1, "stale": 3, "missing": 1}


def collect(monkeypatch, key, payload=PAYLOAD, page="https://www.tesla.com/findus?location=lexingtonkysupercharger"):
    from dataclasses import replace
    saved = {}
    monkeypatch.setattr(main, "settings", replace(main.settings, collector_key="secret"))

    async def catalog():
        return {LEX.location_id: LEX}
    monkeypatch.setattr(main, "_catalog_by_id", catalog)
    monkeypatch.setattr(main.charger_knowledge, "remember_pricing", lambda c, s, source=None: saved.setdefault(c.location_id, s))

    class Req:
        headers = {"X-Collector-Key": key} if key else {}
    result = asyncio.run(main.collect_price(main.CollectedPriceRequest(page_url=page, payload=payload), Req()))
    return result, saved


def test_collected_price_is_parsed_and_saved(monkeypatch):
    result, saved = collect(monkeypatch, "secret")
    assert result["summary"] == "$0.39/kWh" and result["station_name"] == "Lexington, KY"
    assert saved["lexingtonkysupercharger"].bands[0].price_per_kwh == 0.39  # Tesla-vehicle price, not non-Tesla
    assert saved["lexingtonkysupercharger"].fetched_at is not None


def test_collector_rejects_wrong_key_and_bad_data(monkeypatch):
    with pytest.raises(HTTPException) as err:
        collect(monkeypatch, "wrong")
    assert err.value.status_code == 401
    with pytest.raises(HTTPException) as err:
        collect(monkeypatch, "secret", payload={"data": {}})
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        collect(monkeypatch, "secret", page="https://www.tesla.com/findus")
    assert err.value.status_code == 404


class KeyRequest:
    def __init__(self, key):
        self.headers = {"X-Collector-Key": key}


def test_contributor_keys_identify_who_sent_a_price(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(main, "settings", replace(main.settings, collector_key="owner-key", contributor_keys="alice:a-key, bob:b-key"))
    assert main._require_collector_key("owner-key") == "owner"
    assert main._require_collector_key("b-key") == "bob"
    with pytest.raises(HTTPException):
        main._require_collector_key("nope")


def test_parallel_tabs_never_get_the_same_station_and_skip_and_pause(monkeypatch):
    from dataclasses import replace
    import time as _time
    monkeypatch.setattr(main, "settings", replace(main.settings, collector_key="k", collector_pause_minutes=30))
    monkeypatch.setattr(main, "collector_leases", {})
    monkeypatch.setattr(main, "collector_paused_until", 0.0)
    stations = [Charger(id=f"s{i}", location_id=f"s{i}", name=f"S{i}", coordinate=Coordinate(lat=0, lon=0), country="USA") for i in range(8)]

    async def catalog():
        return stations
    monkeypatch.setattr(main.chargers_provider, "all_open", catalog)
    monkeypatch.setattr(main.charger_knowledge, "all_pricing", lambda: {})
    skipped = {}
    monkeypatch.setattr(main.charger_knowledge, "pricing", lambda sid: None)
    monkeypatch.setattr(main.charger_knowledge, "remember_pricing", lambda c, s, source=None: skipped.setdefault(c.location_id, s))

    first = asyncio.run(main.next_stations(KeyRequest("k"), count=5))["items"]
    second = asyncio.run(main.next_stations(KeyRequest("k"), count=5))["items"]
    ids = [i["station_id"] for i in first + second]
    assert len(first) == 5 and len(second) == 3 and len(set(ids)) == 8  # leased stations aren't handed out twice

    asyncio.run(main.skip_station(main.CollectorStationRequest(station_id="s0"), KeyRequest("k")))
    assert skipped["s0"].kind == "unknown" and "s0" not in main.collector_leases

    asyncio.run(main.collector_blocked(KeyRequest("k")))
    paused = asyncio.run(main.next_stations(KeyRequest("k"), count=5))
    assert paused["items"] == [] and paused["paused_until"]


def test_price_export_and_central_import_round_trip(tmp_path):
    from app.chargers.knowledge_store import ChargerKnowledgeStore
    source = ChargerKnowledgeStore(tmp_path / "a.json")
    schedule = PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.39)])
    source.remember_pricing(LEX, schedule, source="collector:owner")
    exported = source.export_prices()["prices"]
    assert set(exported) == {"lexingtonkysupercharger"}

    target = ChargerKnowledgeStore(tmp_path / "b.json")
    junk = {"evil": {"pricing": {"kind": "flat", "bands": "nope"}}, "bad-kind": {"pricing": {"kind": "unknown", "bands": []}}}
    assert target.import_prices({**exported, **junk}, source="central") == 1  # malformed or unpriced entries are ignored
    assert target.pricing("lexingtonkysupercharger").bands[0].price_per_kwh == 0.39
    assert target.pricing("evil") is None
