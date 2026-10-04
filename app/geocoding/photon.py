from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from app.db.cache import CacheDB
from app.models import Coordinate, ResolvedLocation


class Geocoder(ABC):
    @abstractmethod
    async def geocode(self, query: str) -> ResolvedLocation:
        raise NotImplementedError


class PhotonGeocoder(Geocoder):
    def __init__(self, base_url: str, cache: CacheDB, timeout: float, user_agent: str):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent

    async def geocode(self, query: str) -> ResolvedLocation:
        key = query.strip().lower()
        cached = self.cache.get("geocode", key, max_age_seconds=30 * 86400)
        if cached:
            return ResolvedLocation.model_validate(cached)

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(f"{self.base_url}/api/", params={"q": query, "limit": 1})
            response.raise_for_status()
            data = response.json()
        features = data.get("features") or []
        if not features:
            raise ValueError(f"Could not geocode: {query}")
        feature = features[0]
        lon, lat = feature["geometry"]["coordinates"]
        props = feature.get("properties", {})
        parts = [props.get(k) for k in ("name", "city", "state", "country") if props.get(k)]
        result = ResolvedLocation(
            query=query,
            label=", ".join(dict.fromkeys(parts)) or query,
            coordinate=Coordinate(lat=lat, lon=lon),
        )
        self.cache.set("geocode", key, result.model_dump(mode="json"))
        return result
