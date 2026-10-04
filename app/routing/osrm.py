from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import json

import httpx

from app.db.cache import CacheDB
from app.models import Coordinate, RouteSummary

MILES_PER_METER = 0.000621371


class RouteProvider(ABC):
    @abstractmethod
    async def route(self, origin: Coordinate, destination: Coordinate, alternatives: bool = False) -> list[RouteSummary]:
        raise NotImplementedError

    @abstractmethod
    async def table(self, points: list[Coordinate]) -> tuple[list[list[float | None]], list[list[float | None]]]:
        raise NotImplementedError


class OSRMRouteProvider(RouteProvider):
    def __init__(self, base_url: str, cache: CacheDB, timeout: float, user_agent: str):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent

    @staticmethod
    def _coords(points: list[Coordinate]) -> str:
        return ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in points)

    async def route(self, origin: Coordinate, destination: Coordinate, alternatives: bool = False) -> list[RouteSummary]:
        payload = {"o": origin.model_dump(), "d": destination.model_dump(), "a": alternatives}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cached = self.cache.get("route", key, max_age_seconds=7 * 86400)
        if cached:
            return [RouteSummary.model_validate(x) for x in cached]

        coords = self._coords([origin, destination])
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
            "alternatives": "true" if alternatives else "false",
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(f"{self.base_url}/route/v1/driving/{coords}", params=params)
            response.raise_for_status()
            data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(f"OSRM route failed: {data.get('message') or data.get('code')}")
        routes = [
            RouteSummary(
                distance_miles=r["distance"] * MILES_PER_METER,
                duration_minutes=r["duration"] / 60.0,
                geometry=r["geometry"]["coordinates"],
            )
            for r in data["routes"]
        ]
        self.cache.set("route", key, [r.model_dump(mode="json") for r in routes])
        return routes

    async def table(self, points: list[Coordinate]) -> tuple[list[list[float | None]], list[list[float | None]]]:
        payload = [p.model_dump() for p in points]
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cached = self.cache.get("table", key, max_age_seconds=7 * 86400)
        if cached:
            return cached["distances_miles"], cached["durations_minutes"]

        coords = self._coords(points)
        params = {"annotations": "distance,duration"}
        async with httpx.AsyncClient(timeout=max(self.timeout, 30), headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(f"{self.base_url}/table/v1/driving/{coords}", params=params)
            response.raise_for_status()
            data = response.json()
        if data.get("code") != "Ok":
            raise ValueError(f"OSRM table failed: {data.get('message') or data.get('code')}")
        distances = [[None if v is None else v * MILES_PER_METER for v in row] for row in data["distances"]]
        durations = [[None if v is None else v / 60.0 for v in row] for row in data["durations"]]
        self.cache.set("table", key, {"distances_miles": distances, "durations_minutes": durations})
        return distances, durations
