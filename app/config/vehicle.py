"""Personal vehicle assumptions.

Tesla does not publish a simple authoritative usable-battery-kWh figure for every
Model Y trim/model-year combination. The values below are therefore explicit,
editable planning assumptions, not claimed Tesla specifications.
"""

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
