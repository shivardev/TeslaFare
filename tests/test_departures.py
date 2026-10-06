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


def test_trip_request_accepts_vehicle_preset_and_custom_profile():
    preset = TripRequest(from_location="Nashville", to_location="Streetsboro", vehicle_profile_id="model_3_rwd")
    assert preset.vehicle_profile_id == "model_3_rwd"

    custom = TripRequest(
        from_location="Nashville",
        to_location="Streetsboro",
        vehicle_profile_id="custom",
        custom_battery_usable_kwh=82,
        custom_highway_wh_per_mile=285,
    )
    assert custom.custom_battery_usable_kwh == 82

    with pytest.raises(ValidationError):
        TripRequest(from_location="Nashville", to_location="Streetsboro", vehicle_profile_id="custom")
    with pytest.raises(ValidationError):
        TripRequest(from_location="Nashville", to_location="Streetsboro", vehicle_profile_id="roadster")


def test_vehicle_presets_have_their_own_charging_curves_and_old_ids_still_work():
    from app.config import vehicle as v
    lfp = v.planning_profile("model_3_rwd_2024")
    lr = v.planning_profile("model_y_lr_2025")
    sx = v.planning_profile("model_x_lr")
    assert lfp.peak_charge_kw < 200 < lr.peak_charge_kw
    assert sx.peak_charge_kw >= 200
    # Profile ids used before model years were added map to a current preset.
    assert v.planning_profile("model_y_long_range").id == "model_y_lr_2025"
    assert v.planning_profile("model_3_rwd").id == "model_3_rwd_2024"
    assert lr.display_name == "Model Y Long Range (2025+)"


def test_custom_vehicle_peak_power_scales_the_curve():
    from app.config import vehicle as v
    slow = v.planning_profile("custom", 60, 250, custom_peak_kw=120)
    default = v.planning_profile("custom", 60, 250)
    assert slow.peak_charge_kw == 120
    assert default.peak_charge_kw == v.CUSTOM_DEFAULT_PEAK_KW
    assert [lo for lo, _, _ in slow.charging_curve_kw] == [lo for lo, _, _ in default.charging_curve_kw]


def test_model_y_standard_2026_matches_measured_charging():
    from app.config import vehicle as v
    from app.vehicle.charging import ChargingModel
    car = v.planning_profile("model_y_standard_2026")
    charging = ChargingModel(car.battery_usable_kwh, car.charging_curve_kw)
    # Real-world test: 10-80% adds 42.4 kWh in about 31 minutes, peaking around 175 kW.
    assert abs(charging.kwh_between(10, 80) - 42.4) < 1.0
    assert 27 <= charging.minutes_between(10, 80) <= 33
    assert car.peak_charge_kw <= 175
    assert 230 <= car.estimated_highway_range_miles <= 260
