import httpx
import pytest

from app.db.cache import CacheDB
from app.timezones import TimeApiTimezoneProvider


@pytest.mark.asyncio
async def test_timezone_lookup_uses_api_then_sqlite_cache(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["latitude"] == "35.0456"
        assert request.url.params["longitude"] == "-85.3097"
        return httpx.Response(200, json={"timeZone": "America/New_York"})

    cache = CacheDB(tmp_path / "cache.sqlite3")
    provider = TimeApiTimezoneProvider(
        "https://example.test/api/timezone/coordinate",
        cache,
        timeout=5,
        user_agent="test",
        transport=httpx.MockTransport(handler),
    )

    first = await provider.timezone_at(35.0456, -85.3097)
    second = await provider.timezone_at(35.0456, -85.3097)

    assert first == "America/New_York"
    assert second == "America/New_York"
    assert calls == 1


@pytest.mark.asyncio
async def test_timezone_lookup_does_not_guess_on_invalid_response(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"timeZone": "not/a/real-zone"})

    provider = TimeApiTimezoneProvider(
        "https://example.test/api/timezone/coordinate",
        CacheDB(tmp_path / "cache.sqlite3"),
        timeout=5,
        user_agent="test",
        transport=httpx.MockTransport(handler),
    )

    assert await provider.timezone_at(1.0, 2.0) is None
