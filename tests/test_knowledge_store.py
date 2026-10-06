import json

from app.chargers.knowledge_store import ChargerKnowledgeStore
from app.models import Charger, Coordinate, PriceBand, PricingSchedule


def test_knowledge_store_uses_location_id_and_updates_in_place(tmp_path):
    path = tmp_path / "superchargers.json"
    store = ChargerKnowledgeStore(path)
    charger = Charger(
        id="database-id",
        location_id="tesla-location-id",
        name="Example Supercharger",
        coordinate=Coordinate(lat=35.1, lon=-84.9),
        address="1 Main St",
        stalls=12,
        power_kw=250,
        tesla_url="https://tesla.example/tesla-location-id",
    )
    first = PricingSchedule(
        kind="flat",
        bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.31)],
    )
    second = PricingSchedule(
        kind="flat",
        bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.29)],
    )

    store.remember_pricing(charger, first)
    store.remember_pricing(charger, second)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["chargers"]) == ["tesla-location-id"]
    assert payload["chargers"]["tesla-location-id"]["stalls"] == 12
    assert store.pricing("tesla-location-id").bands[0].price_per_kwh == 0.29
    assert store.count() == 1


def test_knowledge_store_keeps_unknown_result_to_avoid_repeated_lookups(tmp_path):
    store = ChargerKnowledgeStore(tmp_path / "superchargers.json")
    charger = Charger(
        id="1",
        location_id="blocked-location",
        name="Blocked",
        coordinate=Coordinate(lat=1, lon=2),
    )
    store.remember_pricing(charger, PricingSchedule(kind="unknown", note="Tesla blocked lookup"))

    saved = store.pricing("blocked-location")
    assert saved is not None
    assert saved.kind == "unknown"


def test_manual_price_updates_known_station(tmp_path):
    store = ChargerKnowledgeStore(tmp_path / "superchargers.json")
    charger = Charger(
        id="1", location_id="manual-location", name="Manual",
        coordinate=Coordinate(lat=1, lon=2),
    )
    store.remember_station(charger)
    schedule = PricingSchedule(
        kind="flat",
        bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=0.37)],
        note="User-entered planning price; not verified by Tesla",
    )

    assert store.remember_manual_pricing("manual-location", schedule) is True
    assert store.pricing("manual-location").bands[0].price_per_kwh == 0.37
    assert store.remember_manual_pricing("missing-location", schedule) is False
