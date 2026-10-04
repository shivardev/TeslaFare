from app.vehicle.charging import ChargingModel


def test_charging_curve_is_not_constant_power():
    model = ChargingModel(75.0, [(0,50,150),(50,80,75),(80,100,30)])
    early = model.minutes_between(10, 30)
    late = model.minutes_between(80, 100)
    assert late > early
    assert model.kwh_between(20, 60) == 30.0
