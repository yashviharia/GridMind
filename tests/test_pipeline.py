import csv
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "appsail"))
from gridcore import battery  # noqa: E402
from gridcore.cleaning import clean_rows  # noqa: E402
from gridcore.features import FEATURE_COLUMNS, build_features  # noqa: E402
from gridcore.forecast import run_forecast  # noqa: E402
from gridcore.quickml_client import QuickMLClient  # noqa: E402

OFFLINE = QuickMLClient(env={})


def sample_rows(n=24, start=9):
    with open(os.path.join(ROOT, "sample_test_input.csv")) as f:
        return list(csv.DictReader(f))[start:start + n]


def test_features_match_training_columns():
    with open(os.path.join(ROOT, "quickml", "quickml_train.csv")) as f:
        header = next(csv.reader(f))
    assert header[:-1] == FEATURE_COLUMNS
    assert header[-1] == "target_demand_kw"


def test_cooling_split_by_period():
    f = build_features({"timestamp": "2015-06-16 19:00:00", "ambient_temp_c": 30, "humidity_pct": 50, "solar_irradiance_wm2": 0})
    assert f["cooling_deg"] == 6 and f["cool_evening"] == 6 and f["cool_day"] == 0 and f["is_evening_peak"] == 1


def test_preserve_rows_keeps_order_and_count():
    raw = [
        {"timestamp": "2015-06-16 02:00:00", "ambient_temp_c": "", "humidity_pct": 60, "solar_irradiance_wm2": 0},
        {"timestamp": "2015-06-16 01:00:00", "ambient_temp_c": 20, "humidity_pct": None, "solar_irradiance_wm2": 0},
        {"timestamp": "2015-06-16 01:00:00", "ambient_temp_c": 22, "humidity_pct": 55, "solar_irradiance_wm2": 0},
    ]
    rows, issues = clean_rows(raw, preserve_rows=True)
    assert [r["timestamp_str"] for r in rows] == [r["timestamp"] for r in raw]
    assert all(r["ambient_temp_c"] is not None and r["humidity_pct"] is not None for r in rows)
    assert any(i["action"] == "imputed" for i in issues)


def test_cleaning_fills_short_gaps_and_rejects_bad_values():
    raw = [
        {"timestamp": "2015-06-16 00:00:00", "ambient_temp_c": 20, "humidity_pct": 60, "solar_irradiance_wm2": 0},
        {"timestamp": "2015-06-16 03:00:00", "ambient_temp_c": 999, "humidity_pct": 60, "solar_irradiance_wm2": 0},
        {"timestamp": "2015-06-16 04:00:00", "ambient_temp_c": 23, "humidity_pct": 60, "solar_irradiance_wm2": 0},
    ]
    rows, issues = clean_rows(raw)
    assert len(rows) == 5
    assert rows[2]["ambient_temp_c"] == 21.5  # interpolated over the rejected 999
    assert {i["action"] for i in issues} >= {"gap filled", "rejected", "imputed"}


def test_forecast_signals_follow_controller():
    out = run_forecast(sample_rows(), client=OFFLINE)
    assert out["model"]["source"] == "local-fallback"
    for h in out["hours"]:
        expect_renewable = h["demand_kw"] > 160 or 18 <= h["hour"] <= 22
        assert (h["grid_mode"] == "RENEWABLE_STORAGE") == expect_renewable
        assert h["overload_alert"] == (h["demand_kw"] >= 200)
        assert h["demand_lo_kw"] <= h["demand_kw"] <= h["demand_hi_kw"]
        assert 0 <= h["soc_pct"] <= 100


def test_battery_energy_balance():
    out = run_forecast(sample_rows(), client=OFFLINE)
    for h in out["hours"]:
        supplied = min(h["solar_kw"], h["demand_kw"]) + h["battery_kw"] + h["grid_kw"] - (h["battery_charge_kw"] - max(0, min(h["solar_kw"] - h["demand_kw"], h["battery_charge_kw"])))
        assert abs(supplied - h["demand_kw"]) < 1.0


def test_recommended_capacity_removes_shortfall():
    out = run_forecast(sample_rows(), client=OFFLINE)
    rec = out["summary"]["battery"]["recommended_capacity_kwh"]
    assert rec and rec > out["battery_config"]["capacity_kwh"]
    out2 = run_forecast(sample_rows(), {"capacity_kwh": rec}, client=OFFLINE)
    assert out2["summary"]["battery"]["shortfall_hours"] == []
