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


def test_visitor_captured_prices_are_accepted_per_trip_only():
    req = main.TripRequest(from_location="A city", to_location="B city", captured_prices={"lexingtonkysupercharger": PAYLOAD})
    schedule = main.parse_tesla_pricing_payload(req.captured_prices["lexingtonkysupercharger"], "x")
    assert schedule.kind == "flat" and schedule.bands[0].price_per_kwh == 0.39  # Tesla-vehicle row, not non-Tesla
    assert main.CAPTURED_PRICE_NOTE.startswith("User-entered")  # treated as a visitor price, never "verified"


def test_price_status_reports_update_times_and_sees_new_prices(tmp_path, monkeypatch):
    from app.chargers.knowledge_store import ChargerKnowledgeStore
    store = ChargerKnowledgeStore(tmp_path / "k.json")
    monkeypatch.setattr(main, "charger_knowledge", store)
    flat = PricingSchedule(kind="flat", bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.39)])
    store.remember_pricing(LEX, flat)
    first = asyncio.run(main.price_status("lexingtonkysupercharger,missing"))["prices"]
    assert list(first) == ["lexingtonkysupercharger"]
    import time as _t
    _t.sleep(0.02)
    store.remember_pricing(LEX, flat)  # re-collected: the cached index must notice the file changed
    second = asyncio.run(main.price_status("lexingtonkysupercharger"))["prices"]
    assert second["lexingtonkysupercharger"] != first["lexingtonkysupercharger"]


class Visitor:
    def __init__(self, ip="203.0.113.7"):
        self.headers = {}
        self.client = type("Client", (), {"host": ip})()


def test_price_session_tracks_trip_stations(monkeypatch, tmp_path):
    from app.chargers.knowledge_store import ChargerKnowledgeStore
    store = ChargerKnowledgeStore(tmp_path / "k.json")
    monkeypatch.setattr(main, "charger_knowledge", store)
    monkeypatch.setattr(main, "price_sessions", {})
    from app.pricing.guards import CommunityGate
    monkeypatch.setattr(main, "community_gate", CommunityGate(tmp_path / "audit.jsonl", 60, 0.5))
    other = Charger(id="o", location_id="other", name="Other", coordinate=Coordinate(lat=0, lon=0), country="USA")

    async def catalog():
        return {LEX.location_id: LEX, other.location_id: other}
    monkeypatch.setattr(main, "_catalog_by_id", catalog)
    session = main._price_session("abcdef123456", create=True)
    session["stations"] = {LEX.location_id}
    state = lambda: asyncio.run(main.price_session_state("abcdef123456"))["stations"]
    assert state()[LEX.location_id]["status"] == "missing"

    asyncio.run(main.price_session_opening("abcdef123456", main.SessionStationsRequest(station_ids=[LEX.location_id, "other"])))
    assert state()[LEX.location_id]["status"] == "opening" and "other" not in state()  # only the trip's stations
    session["opening"][LEX.location_id] = 0  # the 90 s window passed: a blocked/closed tab doesn't stay stuck
    assert state()[LEX.location_id]["status"] == "missing"

    req = lambda **kw: main.SessionCaptureRequest(page_url=f"https://www.tesla.com/findus?location={LEX.location_id}", **kw)
    asyncio.run(main.price_session_captured("abcdef123456", req(failed="Tesla showed Access Denied"), Visitor()))
    assert state()[LEX.location_id] == {"status": "failed", "reason": "Tesla showed Access Denied"}
    result = asyncio.run(main.price_session_captured("abcdef123456", req(payload=PAYLOAD), Visitor()))
    assert result["summary"] == "$0.39/kWh" and result["shared"] == "accepted"
    priced = state()[LEX.location_id]
    assert priced["status"] == "priced" and priced["source"] == "captured"
    assert store.pricing(LEX.location_id).bands[0].price_per_kwh == 0.39  # first report for a station: shared
    assert main._price_versions([LEX], session)[LEX.location_id] == priced["updated_at"]  # plan and session agree

    with pytest.raises(HTTPException):
        asyncio.run(main.price_session_captured("abcdef123456", main.SessionCaptureRequest(page_url="https://www.tesla.com/findus?location=other", payload=PAYLOAD), Visitor()))
    with pytest.raises(HTTPException):
        asyncio.run(main.price_session_state("nosuchsession"))


def payload_with(rate, **extra):
    row = {"feeType": "CHARGING", "uom": "kwh", "vehicleMakeType": "TSLA", "rateBase": rate, "isTou": False, **extra}
    return {"data": {"effectivePricebooks": [row]}}


def test_guardrails_reject_bad_shapes_bounds_and_wrong_station():
    from app.pricing.guards import PriceRejected, check_price_bounds, names_other_station, validate_payload_shape
    for bad in ({"data": {}}, {"data": {"effectivePricebooks": ["x"]}}, payload_with("0.30"),
                payload_with(0.3, startTime="25h"), {"data": {"effectivePricebooks": [{}] * 100}},
                {"data": {"effectivePricebooks": [{"feeType": "CHARGING", "pad": "x" * 70000}]}}):
        with pytest.raises(PriceRejected):
            validate_payload_shape(bad)
    validate_payload_shape(PAYLOAD)
    for rate in (0.01, 3.0):
        with pytest.raises(PriceRejected):
            check_price_bounds(main.parse_tesla_pricing_payload(payload_with(rate), "x"))
    check_price_bounds(main.parse_tesla_pricing_payload(payload_with(0.42), "x"))
    assert names_other_station({"data": {"slug": "other"}}, "lexingtonkysupercharger", {"other", "lexingtonkysupercharger"})


def test_community_gate_holds_big_changes_until_a_second_report_and_rate_limits(tmp_path):
    from app.pricing.guards import CommunityGate
    gate = CommunityGate(tmp_path / "a.jsonl", per_hour=3, change_threshold=0.5)
    parse = lambda rate: main.parse_tesla_pricing_payload(payload_with(rate), "x")
    saved = parse(0.40)
    assert gate.decide("s", parse(0.45), "alice", saved) == "accepted"   # small change: goes live
    assert gate.decide("s", parse(0.10), "alice", saved) == "pending"    # -75%: waits
    assert gate.decide("s", parse(0.10), "alice", saved) == "pending"    # same person again doesn't count
    assert gate.decide("s", parse(0.10), "bob", saved) == "accepted"     # second, independent report
    assert gate.decide("t", parse(0.30), "carol", None) == "accepted"    # first price for a station
    for _ in range(2):
        gate.decide("u", parse(0.30), "carol", None)
    assert gate.decide("v", parse(0.30), "carol", None) == "rate_limited"


def test_owner_can_undo_everything_from_one_contributor(monkeypatch, tmp_path):
    from dataclasses import replace
    from app.chargers.knowledge_store import ChargerKnowledgeStore
    from app.pricing.guards import CommunityGate
    store = ChargerKnowledgeStore(tmp_path / "k.json")
    monkeypatch.setattr(main, "charger_knowledge", store)
    monkeypatch.setattr(main, "community_gate", CommunityGate(tmp_path / "audit.jsonl", 60, 0.5))
    monkeypatch.setattr(main, "settings", replace(main.settings, collector_key="owner-key", community_prices=True))
    good = main.parse_tesla_pricing_payload(payload_with(0.40), "x")
    store.remember_pricing(LEX, good, source="collector:owner")
    vandal = Visitor("198.51.100.9")
    assert main._share_community_price(LEX, main.parse_tesla_pricing_payload(payload_with(0.50), "x"), vandal) == "accepted"
    assert store.pricing(LEX.location_id).bands[0].price_per_kwh == 0.50
    contributor = main._contributor(vandal)
    result = asyncio.run(main.revert_contributor(main.RevertRequest(contributor=contributor), KeyRequest("owner-key")))
    assert result["restored"] == 1
    assert store.pricing(LEX.location_id).bands[0].price_per_kwh == 0.40  # the owner's price is back
    assert any(e["kind"] == "revert" for e in main.community_gate.recent())
