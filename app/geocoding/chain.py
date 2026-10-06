from __future__ import annotations

import logging
import re

from app.geocoding.photon import Geocoder, GeocodingNotFound
from app.models import ResolvedLocation

log = logging.getLogger("teslafare")

_STREET_ADDRESS = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")


def looks_like_street_address(query: str) -> bool:
    return bool(_STREET_ADDRESS.match(query))


class FallbackGeocoder(Geocoder):
    """Tries the address geocoder first for street addresses, the place geocoder first otherwise."""

    def __init__(self, place: Geocoder, address: Geocoder):
        self.place = place
        self.address = address

    async def geocode(self, query: str) -> ResolvedLocation:
        order = [self.address, self.place] if looks_like_street_address(query) else [self.place, self.address]
        errors: list[Exception] = []
        for geocoder in order:
            try:
                return await geocoder.geocode(query)
            except Exception as exc:
                log.info("%s could not resolve %r: %s: %s", type(geocoder).__name__, query, type(exc).__name__, exc)
                errors.append(exc)
        if all(isinstance(e, GeocodingNotFound) for e in errors):
            raise GeocodingNotFound(query)
        raise next(e for e in errors if not isinstance(e, GeocodingNotFound))
