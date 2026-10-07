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
    monkeypatch.setattr(main.charger_knowledge, "remember_pricing", lambda c, s: saved.setdefault(c.location_id, s))

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
