from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.db.cache import CacheDB

log = logging.getLogger(__name__)


class TimezoneProvider(ABC):
    @abstractmethod
    async def timezone_at(self, lat: float, lon: float) -> str | None:
        raise NotImplementedError


class TimeApiTimezoneProvider(TimezoneProvider):
    """Resolve IANA timezones from coordinates using TimeAPI and cache them locally.

    This intentionally avoids the native ``timezonefinder`` dependency so Windows users
    do not need a local C/C++ compiler just to install this personal project.
    """

    def __init__(
        self,
        base_url: str,
        cache: CacheDB,
        timeout: float,
        user_agent: str,
        cache_days: int = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache_seconds = cache_days * 86400
        self.transport = transport

    @staticmethod
    def _cache_key(lat: float, lon: float) -> str:
        # Four decimals is ~10 m of latitude precision, much tighter than needed for
        # timezone boundaries while still producing stable cache keys.
        return f"{lat:.4f},{lon:.4f}"

    @staticmethod
    def _extract_timezone(payload: dict) -> str | None:
        for key in ("timeZone", "timezone", "time_zone", "timeZoneId", "zoneName"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                try:
                    ZoneInfo(candidate)
                except ZoneInfoNotFoundError:
                    continue
                return candidate
        return None

    async def timezone_at(self, lat: float, lon: float) -> str | None:
        key = self._cache_key(lat, lon)
        cached = self.cache.get("timezone", key, max_age_seconds=self.cache_seconds)
        if isinstance(cached, dict) and cached.get("timezone"):
            return str(cached["timezone"])

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                transport=self.transport,
            ) as client:
                response = await client.get(
                    self.base_url,
                    params={"latitude": lat, "longitude": lon},
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Timezone provider returned a non-object JSON response")
            tz = self._extract_timezone(payload)
            if not tz:
                raise ValueError("Timezone provider response did not contain a valid IANA timezone")
            self.cache.set("timezone", key, {"timezone": tz})
            return tz
        except Exception as exc:
            # Timezone failures must not silently turn station-local TOU prices into UTC.
            # Callers can exclude TOU stations when this returns None.
            log.warning("Timezone lookup failed for %.5f,%.5f: %s", lat, lon, exc)
            return None
