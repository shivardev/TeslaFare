from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnergyModel:
    battery_usable_kwh: float
    highway_wh_per_mile: float

    def energy_kwh(self, distance_miles: float) -> float:
        return distance_miles * self.highway_wh_per_mile / 1000.0

    def soc_points(self, distance_miles: float) -> float:
        return self.energy_kwh(distance_miles) / self.battery_usable_kwh * 100.0

    def reachable(self, current_soc: float, distance_miles: float, reserve_soc: float) -> bool:
        return current_soc - self.soc_points(distance_miles) >= reserve_soc - 1e-9
