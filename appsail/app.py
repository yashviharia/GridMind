"""GridMind backend: Flask app served on Catalyst AppSail (API + dashboard, same origin)."""
import csv
import io
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone

# AppSail runs app.py with dependencies vendored next to it; make that explicit.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory

from gridcore import controller, store
from gridcore.cleaning import CleaningError, clean_rows
from gridcore.features import FEATURE_COLUMNS, build_features, load_params
from gridcore.forecast import local_predict, predict_demand, run_forecast
from gridcore.quickml_client import QuickMLClient, QuickMLError

BASE = os.path.dirname(os.path.abspath(__file__))
MAX_ROWS = 2000

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
client = QuickMLClient()
events = deque(maxlen=200)  # in-process system log shown on the dashboard


def log_event(level, message):
    events.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": level, "message": message})


def _load_days(name):
    with open(os.path.join(BASE, "data", name)) as f:
        days = {}
        for r in csv.DictReader(f):
            days.setdefault(r["timestamp"][:10], []).append(r)
    return {d: rs for d, rs in days.items() if len(rs) == 24}


def _scenarios():
    """Representative 24-hour windows (midnight to midnight) for one-click demos.

    Summer days come from the sample test input; winter and spring come from
    the training period (weather columns only) to show Main Grid operation.
    """
    summer = _load_days("sample_test_input.csv")
    reference = _load_days("reference_days.csv")
    by_temp = sorted(summer, key=lambda d: max(float(r["ambient_temp_c"]) for r in summer[d]))
    winter, spring = sorted(reference)
    picks = [
        ("heatwave", "Heatwave day", by_temp[-1], summer),
        ("summer", "Typical summer day", by_temp[len(by_temp) // 2], summer),
        ("spring", "Spring day", spring, reference),
        ("winter", "Winter day", winter, reference),
    ]
    return [
        {"id": i, "label": label, "date": d, "max_temp_c": max(float(r["ambient_temp_c"]) for r in src[d]), "rows": src[d]}
        for i, label, d, src in picks
    ]


SCENARIOS = _scenarios()


def _rows_from_request():
    """Accept a CSV upload (multipart 'file'), raw CSV text, or JSON {rows:[...]}."""
    if "file" in request.files:
        upload = request.files["file"]
        text = upload.read().decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        options = request.form.to_dict()
        battery_cfg = json.loads(options.get("battery") or "{}")
        store.save_upload(upload.filename or "upload.csv", text)
        return rows, battery_cfg, float(options.get("temp_offset_c") or 0), f"CSV upload '{upload.filename}'"
    body = request.get_json(silent=True) or {}
    if "csv" in body:
        rows = list(csv.DictReader(io.StringIO(body["csv"])))
        origin = "pasted CSV"
    elif "scenario" in body:
        match = next((s for s in SCENARIOS if s["id"] == body["scenario"]), None)
        if not match:
            raise CleaningError(f"Unknown scenario {body['scenario']!r}")
        rows, origin = match["rows"], f"scenario '{match['label']}'"
    else:
        rows, origin = body.get("rows") or [], "manual form"
    return rows, body.get("battery") or {}, float(body.get("temp_offset_c") or 0), origin


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/api/status")
def status():
    with open(os.path.join(BASE, "data", "model_report.json")) as f:
        report = json.load(f)
    return jsonify(
        quickml_configured=client.configured,
        quickml_missing=client.missing_settings(),
        storage=store.backend_name(),
        features=FEATURE_COLUMNS,
        controller={
            "renewable_above_kw": controller.RENEWABLE_DEMAND_KW,
            "overload_at_kw": controller.OVERLOAD_DEMAND_KW,
            "peak_window": list(controller.PEAK_WINDOW),
        },
        model_report=report,
    )


@app.get("/api/scenarios")
def scenarios():
    return jsonify([{k: v for k, v in s.items() if k != "rows"} | {"n_rows": len(s["rows"])} for s in SCENARIOS])


@app.get("/api/scenarios/<sid>")
def scenario_rows(sid):
    match = next((s for s in SCENARIOS if s["id"] == sid), None)
    if not match:
        return jsonify(error="unknown scenario"), 404
    return jsonify(match)


@app.post("/api/forecast")
def forecast():
    try:
        rows, battery_cfg, offset, origin = _rows_from_request()
        if len(rows) > MAX_ROWS:
            raise CleaningError(f"Too many rows ({len(rows)}); the limit is {MAX_ROWS}.")
        result = run_forecast(rows, battery_cfg, offset, client)
    except CleaningError as e:
        log_event("error", f"Rejected input: {e}")
        return jsonify(error=str(e)), 400
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        log_event("error", f"Bad request: {e}")
        return jsonify(error=f"Could not read input: {e}"), 400

    s = result["summary"]
    model = result["model"]
    log_event("info", f"Forecast from {origin}: {s['n_hours']} h, peak {s['peak_demand_kw']} kW, model={model['source']}")
    if model["source"] != "quickml":
        log_event("warning", f"QuickML unavailable ({model['error']}); served local mirror model")
    if result["data_quality"]["issues"]:
        log_event("warning", f"Data cleanup applied {len(result['data_quality']['issues'])} fix(es)")
    for t in result["transitions"][1:]:
        log_event("signal", f"{t['timestamp']}: switch {t['from']} -> {t['to']}")
    if s["overload_hours"]:
        log_event("critical", f"Overload alert: {s['overload_hours']} hour(s) >= {controller.OVERLOAD_DEMAND_KW:.0f} kW")
    result["run_id"] = store.save_run(origin, result)
    return jsonify(result)


@app.post("/api/predict")
def predict_contract():
    """Contract endpoint used by predict.py: {"data": [raw rows]} -> {"predictions": [...]}.

    Runs the same cleaning and feature pipeline as the dashboard, then calls
    QuickML. It does not fall back to the local model, so a success here always
    means the numbers came from QuickML.
    """
    body = request.get_json(silent=True) or {}
    data = body.get("data")
    if isinstance(data, dict):
        data = [data]
    if not data:
        return jsonify(error="Body must be {\"data\": [rows]}"), 400
    try:
        rows, _ = clean_rows(data, preserve_rows=True)
        params = load_params()
        features = [build_features(r, params) for r in rows]
        preds, source, _ = predict_demand(features, client, allow_fallback=False)
    except CleaningError as e:
        return jsonify(error=str(e)), 400
    except QuickMLError as e:
        log_event("error", f"/api/predict failed: {e}")
        return jsonify(error=str(e)), 502
    log_event("info", f"/api/predict served {len(preds)} predictions from {source}")
    return jsonify(predictions=[round(max(0.0, p), 3) for p in preds], source=source)


@app.post("/api/explain")
def explain():
    """Why is this hour's forecast what it is? QuickML SHAP when available,
    otherwise exact contributions from the linear mirror model."""
    body = request.get_json(silent=True) or {}
    try:
        rows, _ = clean_rows([body], preserve_rows=True)
    except CleaningError as e:
        return jsonify(error=str(e)), 400
    params = load_params()
    features = build_features(rows[0], params)
    coef = params["fallback_model"]["coefficients"]
    contributions = sorted(
        ({"feature": c, "value": features[c], "contribution_kw": round(coef[c] * features[c], 2)} for c in FEATURE_COLUMNS),
        key=lambda x: -abs(x["contribution_kw"]),
    )
    result = {
        "features": features,
        "linear_contributions": contributions,
        "intercept_kw": params["fallback_model"]["intercept"],
        "local_prediction": round(local_predict(features), 2),
    }
    if client.configured:
        try:
            result["quickml"] = client.explain(features)
        except QuickMLError as e:
            result["quickml_error"] = str(e)
    return jsonify(result)


@app.get("/api/history")
def history():
    return jsonify(runs=store.list_runs(), events=list(events))


@app.get("/api/history/<run_id>")
def history_run(run_id):
    run = store.get_run(run_id)
    return (jsonify(run), 200) if run else (jsonify(error="not found"), 404)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


log_event("info", f"GridMind started; QuickML {'configured' if client.configured else 'NOT configured (using local mirror)'}; storage={store.backend_name()}")

if __name__ == "__main__":
    port = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", 9000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
