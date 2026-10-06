import asyncio

import pytest

from app.geocoding.chain import FallbackGeocoder, looks_like_street_address
from app.geocoding.photon import Geocoder, GeocodingNotFound
from app.models import Coordinate, ResolvedLocation


class Fake(Geocoder):
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, []

    async def geocode(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return ResolvedLocation(query=query, label=self.name, coordinate=Coordinate(lat=1, lon=2))


def test_street_address_detection():
    assert looks_like_street_address("2130 Donaldson Hwy, Hebron, KY 41048")
    assert looks_like_street_address("8540 raspberry way,ootewah, TN")
    assert not looks_like_street_address("Hebron, KY")
    assert not looks_like_street_address("Wingate by Wyndham Streetsboro, Streetsboro, OH")


def test_street_address_uses_address_geocoder_first():
    place, address = Fake("place"), Fake("address")
    result = asyncio.run(FallbackGeocoder(place, address).geocode("2130 Donaldson Hwy, Hebron, KY"))
    assert result.label == "address" and place.calls == []


def test_place_falls_back_to_address_geocoder():
    place, address = Fake("place", error=GeocodingNotFound("x")), Fake("address")
    assert asyncio.run(FallbackGeocoder(place, address).geocode("Hebron, KY")).label == "address"


def test_not_found_everywhere_raises_not_found():
    chain = FallbackGeocoder(Fake("p", error=GeocodingNotFound("q")), Fake("a", error=GeocodingNotFound("q")))
    with pytest.raises(GeocodingNotFound):
        asyncio.run(chain.geocode("nowhere"))


def test_network_error_is_surfaced_over_not_found():
    chain = FallbackGeocoder(Fake("p", error=TimeoutError("slow")), Fake("a", error=GeocodingNotFound("q")))
    with pytest.raises(TimeoutError):
        asyncio.run(chain.geocode("Hebron, KY"))
