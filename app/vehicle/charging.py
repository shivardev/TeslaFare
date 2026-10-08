from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargingModel:
    battery_usable_kwh: float
    curve_kw: list[tuple[float, float, float]]

    def kwh_between(self, start_soc: float, end_soc: float) -> float:
        return max(0.0, end_soc - start_soc) / 100.0 * self.battery_usable_kwh

    def segments(self, start_soc: float, end_soc: float) -> list[tuple[float, float]]:
        """(minutes, kW) for each part of the charging curve between two SOCs."""
        parts = []
        for lo, hi, kw in self.curve_kw:
            overlap_lo, overlap_hi = max(start_soc, lo), min(end_soc, hi)
            if overlap_hi > overlap_lo:
                kwh = (overlap_hi - overlap_lo) / 100.0 * self.battery_usable_kwh
                parts.append((kwh / kw * 60.0, kw))
        return parts

    def minutes_between(self, start_soc: float, end_soc: float) -> float:
        if end_soc <= start_soc:
            return 0.0
        minutes = 0.0
        for lo, hi, kw in self.curve_kw:
            overlap_lo = max(start_soc, lo)
            overlap_hi = min(end_soc, hi)
            if overlap_hi <= overlap_lo:
                continue
            kwh = (overlap_hi - overlap_lo) / 100.0 * self.battery_usable_kwh
            minutes += kwh / kw * 60.0
        return minutes
