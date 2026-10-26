"""Forecast pipeline: clean -> features -> QuickML -> controller -> battery."""
import bisect
import time

from . import battery, controller
from .cleaning import clean_rows
from .features import FEATURE_COLUMNS, build_features, load_params
from .quickml_client import QuickMLClient, QuickMLError

WEATHER_TERMS = ["cooling_deg", "heating_deg", "cool_night", "cool_morning", "cool_day", "cool_evening"]


def local_predict(features):
    """Linear fallback with the same features and form as the QuickML model."""
    m = load_params()["fallback_model"]
    return m["intercept"] + sum(m["coefficients"][c] * features[c] for c in FEATURE_COLUMNS)


def weather_load(features):
    """kW attributable to heating/cooling, from the linear model's weather terms."""
    coef = load_params()["fallback_model"]["coefficients"]
    return sum(coef[c] * features[c] for c in WEATHER_TERMS)


def exceedance_probability(pred, threshold, residuals):
    """P(actual >= threshold) using the empirical distribution of holdout errors."""
    need = threshold - pred  # actual = pred + error, so error must be >= need
    idx = bisect.bisect_left(residuals, need)
    return round(1 - idx / len(residuals), 3)


def predict_demand(feature_rows, client, allow_fallback=True):
    """Returns (predictions, source, error_message)."""
    if client.configured:
        try:
            return client.predict_many(feature_rows), "quickml", None
        except QuickMLError as e:
            if not allow_fallback:
                raise
            return [local_predict(f) for f in feature_rows], "local-fallback", str(e)
    if not allow_fallback:
        raise QuickMLError("QuickML is not configured: missing " + ", ".join(client.missing_settings()))
    return [local_predict(f) for f in feature_rows], "local-fallback", "QuickML not configured"


def run_forecast(raw_rows, battery_settings=None, temp_offset_c=0.0, client=None):
    started = time.time()
    client = client or QuickMLClient()
    rows, issues = clean_rows(raw_rows)

    if temp_offset_c:
        # What-if scenario: shift temperature, keep humidity physically consistent
        # with the inverse relationship seen in the training data (~-1.2 %/degC).
        for r in rows:
            r["ambient_temp_c"] = round(r["ambient_temp_c"] + temp_offset_c, 3)
            r["humidity_pct"] = round(min(100.0, max(0.0, r["humidity_pct"] - 1.19 * temp_offset_c)), 3)

    params = load_params()
    feature_rows = [build_features({**r, "timestamp": r["timestamp"]}, params) for r in rows]
    preds, source, model_error = predict_demand(feature_rows, client)

    res = params["residuals"]
    sample = res["sample"]
    hours = []
    for r, f, p in zip(rows, feature_rows, preds):
        p = max(0.0, p)
        hour = r["timestamp"].hour
        signal = controller.evaluate(hour, p)
        wl = weather_load(f)
        hours.append({
            "timestamp": r["timestamp_str"],
            "hour": hour,
            "ambient_temp_c": r["ambient_temp_c"],
            "humidity_pct": r["humidity_pct"],
            "solar_irradiance_wm2": r["solar_irradiance_wm2"],
            "demand_kw": round(p, 2),
            "demand_lo_kw": round(max(0.0, p + res["q05"]), 2),
            "demand_hi_kw": round(p + res["q95"], 2),
            "base_load_kw": round(max(0.0, p - wl), 2),
            "weather_load_kw": round(wl, 2),
            "p_overload": exceedance_probability(p, controller.OVERLOAD_DEMAND_KW, sample),
            "p_above_160": exceedance_probability(p, controller.RENEWABLE_DEMAND_KW, sample),
            **signal,
        })

    cfg = battery.BatteryConfig.from_dict(battery_settings)
    sim = battery.simulate(hours, cfg)
    sim["summary"]["recommended_capacity_kwh"] = battery.required_capacity(hours, cfg)
    for h, s in zip(hours, sim["steps"]):
        h.update({k: v for k, v in s.items() if k != "timestamp"})

    alerts = _alerts(hours, sim["summary"])
    demand = [h["demand_kw"] for h in hours]
    peak = max(hours, key=lambda h: h["demand_kw"])
    return {
        "hours": hours,
        "transitions": controller.transitions(hours),
        "alerts": alerts,
        "summary": {
            "n_hours": len(hours),
            "window_start": hours[0]["timestamp"],
            "window_end": hours[-1]["timestamp"],
            "peak_demand_kw": peak["demand_kw"],
            "peak_at": peak["timestamp"],
            "mean_demand_kw": round(sum(demand) / len(demand), 2),
            "energy_kwh": round(sum(demand), 1),
            "renewable_hours": sum(h["grid_mode"] == controller.MODE_RENEWABLE for h in hours),
            "overload_hours": sum(h["overload_alert"] for h in hours),
            "at_risk_hours": sum(1 for h in hours if not h["overload_alert"] and h["p_overload"] >= 0.2),
            "battery": sim["summary"],
            "temp_offset_c": temp_offset_c,
        },
        "battery_config": sim["config"],
        "model": {
            "source": source,
            "error": model_error,
            "interval": [res["q05"], res["q95"]],
            "holdout_rmse": res["rmse"],
        },
        "data_quality": {"issues": issues, "n_input_rows": len(raw_rows), "n_clean_rows": len(rows)},
        "elapsed_ms": round(1000 * (time.time() - started)),
    }


def _alerts(hours, bsum):
    alerts = []
    overloads = [h for h in hours if h["overload_alert"]]
    if overloads:
        worst = max(overloads, key=lambda h: h["demand_kw"])
        alerts.append({
            "level": "critical",
            "title": f"Critical overload: {len(overloads)} hour(s) at or above {controller.OVERLOAD_DEMAND_KW:.0f} kW",
            "detail": f"Peak {worst['demand_kw']:.1f} kW at {worst['timestamp']}. Hours: " + ", ".join(h["timestamp"][11:16] for h in overloads),
        })
    near = [h for h in hours if not h["overload_alert"] and h["p_overload"] >= 0.2]
    if near:
        alerts.append({
            "level": "warning",
            "title": f"{len(near)} hour(s) could still overload",
            "detail": "Forecast is below 200 kW but within model error: " + ", ".join(f"{h['timestamp'][11:16]} ({round(100 * h['p_overload'])}%)" for h in near),
        })
    if bsum["shortfall_hours"]:
        alerts.append({
            "level": "warning",
            "title": "Battery cannot cover every storage-mode hour",
            "detail": f"Grid backup needed at {', '.join(t[11:16] for t in bsum['shortfall_hours'])}; minimum state of charge {bsum['min_soc_pct']}%. " + (f"A {bsum['recommended_capacity_kwh']:,.0f} kWh battery would cover the whole window." if bsum.get("recommended_capacity_kwh") else "The battery's power limit is the bottleneck: raise max power."),
        })
    if not alerts:
        alerts.append({"level": "ok", "title": "All clear", "detail": "No overloads forecast and the battery covers every storage-mode hour."})
    return alerts
