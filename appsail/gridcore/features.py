"""Feature engineering shared by training, the backend, and predict.py.

This is the single source of truth for the model's input columns. The QuickML
training CSV is generated with `build_features`, and every inference path calls
the same function, so training and serving can never drift apart.

Pure Python on purpose: the AppSail backend ships without numpy/pandas.
"""
import json
import os
from datetime import datetime

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "model_params.json")

# Demand starts responding to heat above this temperature (found by grid search
# on a time-ordered holdout; 23 and 25 are both clearly worse).
COOLING_THRESHOLD_C = 24.0
HEATING_THRESHOLD_C = 10.0

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
RAW_COLUMNS = ["timestamp", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]

# Columns sent to QuickML, in order. Keys must match the training CSV exactly.
FEATURE_COLUMNS = [
    "ambient_temp_c",
    "humidity_pct",
    "solar_irradiance_wm2",
    "hour_base_load",
    "cooling_deg",
    "heating_deg",
    "cool_night",
    "cool_morning",
    "cool_day",
    "cool_evening",
    "is_evening_peak",
]


def day_period(hour):
    """Demand regime for an hour of day, matching the load-profile steps in the data."""
    if 6 <= hour <= 9:
        return "morning"
    if 10 <= hour <= 17:
        return "day"
    if 18 <= hour <= 22:
        return "evening"
    return "night"


_params_cache = None


def load_params():
    global _params_cache
    if _params_cache is None:
        with open(PARAMS_PATH) as f:
            _params_cache = json.load(f)
    return _params_cache


def parse_timestamp(value):
    if isinstance(value, datetime):
        return value
    return datetime.strptime(str(value).strip(), TIMESTAMP_FORMAT)


def build_features(row, params=None):
    """Turn one raw telemetry row into the QuickML feature dict."""
    params = params or load_params()
    hour = parse_timestamp(row["timestamp"]).hour
    temp = float(row["ambient_temp_c"])
    cooling = max(0.0, temp - COOLING_THRESHOLD_C)
    period = day_period(hour)

    features = {
        "ambient_temp_c": round(temp, 4),
        "humidity_pct": round(float(row["humidity_pct"]), 4),
        "solar_irradiance_wm2": round(float(row["solar_irradiance_wm2"]), 4),
        # Typical demand for this hour on a mild day: a fixed lookup learned from
        # the training set, so it is a deterministic transform of the hour.
        "hour_base_load": params["hour_base_load"][str(hour)],
        "cooling_deg": round(cooling, 4),
        "heating_deg": round(max(0.0, HEATING_THRESHOLD_C - temp), 4),
        "is_evening_peak": int(period == "evening"),
    }
    for p in ("night", "morning", "day", "evening"):
        features["cool_" + p] = round(cooling if period == p else 0.0, 4)
    return {k: features[k] for k in FEATURE_COLUMNS}
