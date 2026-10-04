from app.vehicle.energy import EnergyModel


def test_energy_and_soc_calculation():
    model = EnergyModel(battery_usable_kwh=75.0, highway_wh_per_mile=250.0)
    assert model.energy_kwh(100) == 25.0
    assert round(model.soc_points(100), 3) == 33.333


def test_soc_feasibility_respects_reserve():
    model = EnergyModel(75.0, 250.0)
    assert model.reachable(50.0, 100.0, 10.0)
    assert not model.reachable(40.0, 100.0, 10.0)
