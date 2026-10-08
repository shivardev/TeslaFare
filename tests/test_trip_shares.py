from datetime import datetime, timezone

from app.db.cache import CacheDB


def test_trip_share_round_trip(tmp_path):
    db = CacheDB(tmp_path / "cache.sqlite3")

    expires_at = db.create_trip_share("trip-id", {"selected": "12:30"}, ttl_days=7)
    saved = db.get_trip_share("trip-id")

    assert saved is not None
    payload, stored_expiry = saved
    assert payload == {"selected": "12:30"}
    assert stored_expiry == expires_at
    assert expires_at > datetime.now(timezone.utc)


def test_expired_trip_share_is_not_returned(tmp_path):
    db = CacheDB(tmp_path / "cache.sqlite3")
    db.create_trip_share("expired", {"value": 1}, ttl_days=-1)

    assert db.get_trip_share("expired") is None
