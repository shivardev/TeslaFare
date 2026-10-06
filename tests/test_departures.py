from datetime import datetime

import pytest
from pydantic import ValidationError

from app.main import REPLAY_TRIPS, TripRequest, _departure_candidates


def test_departure_window_centers_on_desired_local_time():
    desired = datetime(2026, 10, 2, 2, 0)
    options = _departure_candidates("America/Chicago", desired, window_hours=12)

    assert len(options) == 25
    assert options[0].isoformat() == "2026-10-01T20:00:00-05:00"
    assert options[12].isoformat() == "2026-10-02T02:00:00-05:00"
    assert options[-1].isoformat() == "2026-10-02T08:00:00-05:00"


def test_recent_trip_replay_fixture_matches_observed_csv_totals():
    replay = REPLAY_TRIPS["oct_2026_nashville_streetsboro"]
    assert replay["actual_cost"] == 38.35
    assert round(replay["actual_kwh"], 4) == 130.8516
    assert [station[0] for station in replay["stations"]] == [
        "bowlinggreensupercharger",
        "louisvillesupercharger",
        "florencesupercharger",
        "mtgileadsupercharger",
    ]


def test_trip_request_accepts_current_battery_percentage():
    request = TripRequest(from_location="Nashville", to_location="Streetsboro", starting_soc=43)
    assert request.starting_soc == 43
    with pytest.raises(ValidationError):
        TripRequest(from_location="Nashville", to_location="Streetsboro", starting_soc=0)


def test_trip_request_accepts_user_reserve_preferences():
    request = TripRequest(
        from_location="Nashville",
        to_location="Streetsboro",
        min_charger_soc=5,
        destination_soc=15,
    )
    assert request.min_charger_soc == 5
    assert request.destination_soc == 15
    with pytest.raises(ValidationError):
        TripRequest(from_location="Nashville", to_location="Streetsboro", min_charger_soc=51)
