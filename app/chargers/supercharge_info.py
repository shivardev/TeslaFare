from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import httpx

from app.db.cache import CacheDB
from app.models import Charger, Coordinate

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(a: Coordinate, b: Coordinate) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def _project_xy(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    """Cheap local projection in miles, only for geographic pre-filtering."""
    x = math.radians(lon) * math.cos(math.radians(ref_lat)) * EARTH_RADIUS_MILES
    y = math.radians(lat) * EARTH_RADIUS_MILES
    return x, y


def _sample_geometry(geometry: list[list[float]], spacing_miles: float = 4.0) -> list[list[float]]:
    """Reduce a dense OSRM polyline before corridor filtering.

    Corridor filtering is only a first-pass geographic filter; all real trip feasibility
    later uses OSRM road distances/times. Keeping a point every few route miles makes
    this phase fast without materially affecting a 30-60 mile corridor.
    """
    if len(geometry) <= 2:
        return geometry
    sampled = [geometry[0]]
    last = Coordinate(lat=geometry[0][1], lon=geometry[0][0])
    since_last = 0.0
    for lon, lat in geometry[1:-1]:
        current = Coordinate(lat=lat, lon=lon)
        since_last += haversine_miles(last, current)
        last = current
        if since_last >= spacing_miles:
            sampled.append([lon, lat])
            since_last = 0.0
    sampled.append(geometry[-1])
    return sampled


@dataclass(slots=True)
class _CorridorIndex:
    ref_lat: float
    points_xy: list[tuple[float, float]]
    segment_lengths: list[float]
    cumulative: list[float]
    total: float
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    @classmethod
    def build(cls, geometry: list[list[float]], corridor_miles: float) -> "_CorridorIndex":
        sampled = _sample_geometry(geometry)
        if len(sampled) < 2:
            raise ValueError("Route geometry needs at least two points")

        lats = [p[1] for p in sampled]
        lons = [p[0] for p in sampled]
        ref_lat = sum(lats) / len(lats)
        points_xy = [_project_xy(lat, lon, ref_lat) for lon, lat in sampled]

        segment_lengths: list[float] = []
        cumulative = [0.0]
        for (ax, ay), (bx, by) in zip(points_xy, points_xy[1:]):
            seg = math.hypot(bx - ax, by - ay)
            segment_lengths.append(seg)
            cumulative.append(cumulative[-1] + seg)
        total = cumulative[-1] or 1.0

        lat_pad = corridor_miles / 69.0
        lon_scale = max(0.2, math.cos(math.radians(ref_lat)))
        lon_pad = corridor_miles / (69.0 * lon_scale)
        return cls(
            ref_lat=ref_lat,
            points_xy=points_xy,
            segment_lengths=segment_lengths,
            cumulative=cumulative,
            total=total,
            min_lat=min(lats) - lat_pad,
            max_lat=max(lats) + lat_pad,
            min_lon=min(lons) - lon_pad,
            max_lon=max(lons) + lon_pad,
        )

    def in_bbox(self, point: Coordinate) -> bool:
        return (
            self.min_lat <= point.lat <= self.max_lat
            and self.min_lon <= point.lon <= self.max_lon
        )

    def distance_progress(self, point: Coordinate) -> tuple[float, float]:
        px, py = _project_xy(point.lat, point.lon, self.ref_lat)
        best_dist = float("inf")
        best_progress = 0.0
        for i, ((ax, ay), (bx, by)) in enumerate(zip(self.points_xy, self.points_xy[1:])):
            vx, vy = bx - ax, by - ay
            wx, wy = px - ax, py - ay
            vv = vx * vx + vy * vy
            t = 0.0 if vv == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
            qx, qy = ax + t * vx, ay + t * vy
            dist = math.hypot(px - qx, py - qy)
            if dist < best_dist:
                best_dist = dist
                best_progress = (self.cumulative[i] + t * self.segment_lengths[i]) / self.total
        return best_dist, best_progress


def point_to_polyline_miles(point: Coordinate, geometry: list[list[float]]) -> tuple[float, float]:
    """Compatibility helper used by tests/other callers."""
    if len(geometry) < 2:
        return float("inf"), 0.0
    index = _CorridorIndex.build(geometry, corridor_miles=0.0)
    return index.distance_progress(point)


class SuperchargeInfoProvider:
    def __init__(self, url: str, cache: CacheDB, timeout: float, user_agent: str, tesla_base_url: str):
        self.url = url
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent
        self.tesla_base_url = tesla_base_url.rstrip("/")

    async def all_open(self) -> list[Charger]:
        cached = self.cache.get("chargers", "allSites", max_age_seconds=24 * 3600)
        if cached is None:
            async with httpx.AsyncClient(timeout=max(self.timeout, 30), headers={"User-Agent": self.user_agent}) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                cached = response.json()
            self.cache.set("chargers", "allSites", cached)

        chargers: list[Charger] = []
        for raw in cached:
            status = str(raw.get("status", "")).upper()
            if not (status.startswith("OPEN") or status == "EXPANDING"):
                continue
            gps = raw.get("gps") or {}
            lat, lon = gps.get("latitude"), gps.get("longitude")
            location_id = str(raw.get("locationId") or "").strip()
            if lat is None or lon is None or not location_id:
                continue
            address = raw.get("address") or {}
            address_text = ", ".join(
                str(address.get(k)) for k in ("street", "city", "state", "zip") if address.get(k)
            )
            chargers.append(
                Charger(
                    id=str(raw.get("id") or location_id),
                    location_id=location_id,
                    name=str(raw.get("name") or location_id),
                    coordinate=Coordinate(lat=float(lat), lon=float(lon)),
                    address=address_text,
                    status=status,
                    stalls=raw.get("stallCount"),
                    power_kw=raw.get("powerKilowatt"),
                    tesla_url=f"{self.tesla_base_url}/{location_id}",
                )
            )
        return chargers

    @staticmethod
    def corridor_candidates(
        chargers: Iterable[Charger],
        geometry: list[list[float]],
        corridor_miles: float,
        max_candidates: int,
    ) -> list[Charger]:
        if len(geometry) < 2:
            return []
        index = _CorridorIndex.build(geometry, corridor_miles)
        candidates: list[Charger] = []
        for charger in chargers:
            # Very cheap rejection before computing point-to-route distance.
            if not index.in_bbox(charger.coordinate):
                continue
            distance, progress = index.distance_progress(charger.coordinate)
            if distance <= corridor_miles:
                c = charger.model_copy(deep=True)
                c.corridor_distance_miles = distance
                c.route_progress = progress
                candidates.append(c)

        # Preserve longitudinal coverage first, then favor closer-to-route sites.
        candidates.sort(key=lambda c: (c.route_progress, c.corridor_distance_miles))
        if len(candidates) <= max_candidates:
            return candidates

        # One slot per equal slice of the route (closest site in each slice) so no stretch of the
        # route is left without a charger, then fill the remaining slots with the closest sites.
        # Never truncate by progress: that would drop the end of the route.
        bins = max_candidates
        selected: dict[str, Charger] = {}
        for b in range(bins):
            lo, hi = b / bins, (b + 1) / bins
            bucket = [c for c in candidates if lo <= c.route_progress <= hi and c.id not in selected]
            if bucket:
                best = min(bucket, key=lambda x: x.corridor_distance_miles)
                selected[best.id] = best
        for c in sorted(candidates, key=lambda x: x.corridor_distance_miles):
            if len(selected) >= max_candidates:
                break
            selected.setdefault(c.id, c)
        return sorted(selected.values(), key=lambda c: c.route_progress)
