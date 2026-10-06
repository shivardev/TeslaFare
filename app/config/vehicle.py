"""Vehicle planning profiles.

Tesla does not publish a simple authoritative usable-battery-kWh figure for every
Model Y trim/model-year combination. The values below are therefore explicit,
editable planning assumptions, not claimed Tesla specifications.
"""

from dataclasses import dataclass

# Initial personal MVP target: 2026 Tesla Model Y Standard RWD Juniper.
# EDIT THESE to match your own observed car data.
BATTERY_USABLE_KWH = 75.0  # planning assumption; verify/adjust for your vehicle
HIGHWAY_WH_PER_MILE = 260.0  # planning assumption; intentionally simple V1 model
STARTING_SOC = 100.0
MIN_CHARGER_SOC = 10.0
DESTINATION_SOC = 10.0
MAX_PREFERRED_CHARGE_SOC = 85.0
ABSOLUTE_MAX_CHARGE_SOC = 100.0

# Approximate charging curve in kW by SOC band. This is deliberately isolated
# and configurable, not a claim that the car will always achieve these speeds.
CHARGING_CURVE_KW = [
    (0.0, 20.0, 170.0),
    (20.0, 40.0, 150.0),
    (40.0, 60.0, 115.0),
    (60.0, 75.0, 85.0),
    (75.0, 85.0, 60.0),
    (85.0, 95.0, 38.0),
    (95.0, 100.0, 20.0),
]


# Charging curve shapes (SOC band -> kW). Approximate Supercharger V3 behaviour; real sessions vary
# with battery temperature, stall sharing and charger version.
CURVE_NCA_250 = [  # 3/Y Long Range & Performance
    (0.0, 10.0, 200.0), (10.0, 30.0, 240.0), (30.0, 40.0, 190.0), (40.0, 50.0, 150.0),
    (50.0, 60.0, 120.0), (60.0, 70.0, 95.0), (70.0, 80.0, 75.0), (80.0, 90.0, 50.0), (90.0, 100.0, 25.0),
]
CURVE_LFP_170 = [  # LFP-pack RWD cars: lower peak, flatter middle
    (0.0, 20.0, 165.0), (20.0, 40.0, 145.0), (40.0, 60.0, 115.0), (60.0, 75.0, 85.0),
    (75.0, 85.0, 60.0), (85.0, 95.0, 38.0), (95.0, 100.0, 20.0),
]
CURVE_MY_STANDARD_175 = [  # 2026 Model Y Standard: ~175 kW peak, <100 kW by ~45%, 10-80% in ~30 min
    (0.0, 10.0, 140.0), (10.0, 25.0, 165.0), (25.0, 35.0, 135.0), (35.0, 45.0, 105.0), (45.0, 55.0, 85.0),
    (55.0, 65.0, 70.0), (65.0, 80.0, 50.0), (80.0, 90.0, 35.0), (90.0, 100.0, 20.0),
]
CURVE_SX_250 = [  # Model S/X 2021+ (larger pack holds high power a little longer)
    (0.0, 20.0, 230.0), (20.0, 40.0, 200.0), (40.0, 50.0, 160.0), (50.0, 60.0, 130.0),
    (60.0, 70.0, 105.0), (70.0, 80.0, 80.0), (80.0, 90.0, 55.0), (90.0, 100.0, 25.0),
]


def scaled_curve(curve: list[tuple[float, float, float]], peak_kw: float) -> list[tuple[float, float, float]]:
    """The same curve shape scaled to a different peak charging power."""
    factor = peak_kw / max(kw for _, _, kw in curve)
    return [(lo, hi, round(kw * factor, 1)) for lo, hi, kw in curve]


@dataclass(frozen=True)
class VehicleProfile:
    id: str
    label: str
    battery_usable_kwh: float
    highway_wh_per_mile: float
    charging_curve_kw: list[tuple[float, float, float]]
    model: str = "Custom"
    years: str = ""

    @property
    def estimated_highway_range_miles(self) -> float:
        return self.battery_usable_kwh * 1000 / self.highway_wh_per_mile

    @property
    def peak_charge_kw(self) -> float:
        return max(kw for _, _, kw in self.charging_curve_kw)

    @property
    def display_name(self) -> str:
        return f"{self.label} ({self.years})" if self.years else self.label


# Rough planning estimates, not Tesla or EPA specifications. Wheels, weather, speed and battery
# age change real values a lot; owners get the best plans from "Custom" with their own numbers.
VEHICLE_PROFILES = {
    profile.id: profile for profile in [
        VehicleProfile("model_3_rwd_2021", "Model 3 RWD", 57.5, 230.0, CURVE_LFP_170, "Model 3", "2021–2023"),
        VehicleProfile("model_3_rwd_2024", "Model 3 RWD", 57.5, 220.0, CURVE_LFP_170, "Model 3", "2024+"),
        VehicleProfile("model_3_lr_2021", "Model 3 Long Range AWD", 75.0, 245.0, CURVE_NCA_250, "Model 3", "2021–2023"),
        VehicleProfile("model_3_lr_2024", "Model 3 Long Range", 75.0, 235.0, CURVE_NCA_250, "Model 3", "2024+"),
        VehicleProfile("model_3_performance", "Model 3 Performance", 75.0, 275.0, CURVE_NCA_250, "Model 3", "2021+"),
        VehicleProfile("model_y_rwd_2023", "Model Y RWD", 60.0, 255.0, CHARGING_CURVE_KW, "Model Y", "2023–2025"),
        # ~60.5 kWh usable (a 10-80% test added 42.4 kWh), 175 kW peak; EPA 321 mi on 18" wheels.
        VehicleProfile("model_y_standard_2026", "Model Y Standard", 60.5, 250.0, CURVE_MY_STANDARD_175, "Model Y", "2026"),
        VehicleProfile("model_y_lr_2020", "Model Y Long Range AWD", 75.0, 270.0, CURVE_NCA_250, "Model Y", "2020–2024"),
        VehicleProfile("model_y_lr_2025", "Model Y Long Range", 75.0, 260.0, CURVE_NCA_250, "Model Y", "2025+"),
        VehicleProfile("model_y_performance", "Model Y Performance", 75.0, 295.0, CURVE_NCA_250, "Model Y", "2020+"),
        VehicleProfile("model_s_lr", "Model S Long Range", 95.0, 285.0, CURVE_SX_250, "Model S", "2021+"),
        VehicleProfile("model_s_plaid", "Model S Plaid", 95.0, 310.0, CURVE_SX_250, "Model S", "2021+"),
        VehicleProfile("model_x_lr", "Model X Long Range", 95.0, 335.0, CURVE_SX_250, "Model X", "2021+"),
        VehicleProfile("model_x_plaid", "Model X Plaid", 95.0, 350.0, CURVE_SX_250, "Model X", "2021+"),
    ]
}
# Earlier profile ids, kept so saved links and requests keep working.
PROFILE_ALIASES = {
    "model_3_rwd": "model_3_rwd_2024",
    "model_3_long_range": "model_3_lr_2024",
    "model_y_rwd": "model_y_rwd_2023",
    "model_y_long_range": "model_y_lr_2025",
    "model_s": "model_s_lr",
    "model_x": "model_x_lr",
}
DEFAULT_VEHICLE_PROFILE_ID = "model_y_standard_2026"
CUSTOM_DEFAULT_PEAK_KW = 250.0


def profile_groups() -> list[tuple[str, list[VehicleProfile]]]:
    """Presets grouped by model, in display order, for the vehicle picker."""
    groups: dict[str, list[VehicleProfile]] = {}
    for profile in VEHICLE_PROFILES.values():
        groups.setdefault(profile.model, []).append(profile)
    return list(groups.items())


def planning_profile(
    profile_id: str,
    custom_kwh: float | None = None,
    custom_wh_per_mile: float | None = None,
    custom_peak_kw: float | None = None,
) -> VehicleProfile:
    if profile_id == "custom":
        if custom_kwh is None or custom_wh_per_mile is None:
            raise ValueError("Custom vehicles require usable battery capacity and highway efficiency")
        curve = scaled_curve(CURVE_NCA_250, custom_peak_kw or CUSTOM_DEFAULT_PEAK_KW)
        return VehicleProfile("custom", "Custom vehicle", custom_kwh, custom_wh_per_mile, curve)
    try:
        return VEHICLE_PROFILES[PROFILE_ALIASES.get(profile_id, profile_id)]
    except KeyError as exc:
        raise ValueError(f"Unknown vehicle profile: {profile_id}") from exc
