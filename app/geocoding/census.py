from __future__ import annotations

import httpx

from app.db.cache import CacheDB
from app.geocoding.photon import Geocoder, GeocodingNotFound
from app.models import Coordinate, ResolvedLocation


def _label(matched: str) -> str:
    # "2130 DONALDSON HWY, HEBRON, KY, 41048" -> "2130 Donaldson Hwy, Hebron, KY, 41048"
    parts = [p.strip() for p in matched.split(",")]
    return ", ".join(p.title() if i < len(parts) - 2 else p for i, p in enumerate(parts))


class CensusGeocoder(Geocoder):
    """US Census Bureau one-line address geocoder: strong on US street addresses, no API key."""

    def __init__(self, base_url: str, cache: CacheDB, timeout: float, user_agent: str):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent

    async def geocode(self, query: str) -> ResolvedLocation:
        key = query.strip().lower()
        cached = self.cache.get("geocode_census", key, max_age_seconds=30 * 86400)
        if cached:
            return ResolvedLocation.model_validate(cached)

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(
                f"{self.base_url}/geocoder/locations/onelineaddress",
                params={"address": query, "benchmark": "Public_AR_Current", "format": "json"},
            )
            response.raise_for_status()
            data = response.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            raise GeocodingNotFound(query)
        match = matches[0]
        coords = match["coordinates"]
        result = ResolvedLocation(
            query=query,
            label=_label(str(match.get("matchedAddress") or query)),
            coordinate=Coordinate(lat=coords["y"], lon=coords["x"]),
        )
        self.cache.set("geocode_census", key, result.model_dump(mode="json"))
        return result
