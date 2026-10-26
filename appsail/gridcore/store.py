"""Persistence for forecast runs and uploaded CSVs.

On Catalyst (GRIDMIND_STORAGE=catalyst):
  Data Store table  GRIDMIND_TABLE (default ForecastRuns)  one summary row per run
  Stratus bucket    GRIDMIND_BUCKET                        full run JSON + raw uploads
Anywhere else, or if a Catalyst call fails, it keeps the last runs in memory so
the dashboard always works. Storage failures never fail a forecast.
"""
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

log = logging.getLogger("gridmind.store")

USE_CATALYST = os.environ.get("GRIDMIND_STORAGE", "memory").lower() == "catalyst"
TABLE = os.environ.get("GRIDMIND_TABLE", "ForecastRuns")
BUCKET = os.environ.get("GRIDMIND_BUCKET", "")
MEMORY_LIMIT = 50

_memory = OrderedDict()
_lock = threading.Lock()
_last_error = None


def backend_name():
    if not USE_CATALYST:
        return "memory"
    return "catalyst-datastore+stratus" + (f" (degraded: {_last_error})" if _last_error else "")


def _catalyst_app():
    import zcatalyst_sdk
    from flask import request

    return zcatalyst_sdk.initialize(req=request, scope="admin")


def _note_failure(what, err):
    global _last_error
    _last_error = f"{what}: {err}"[:200]
    log.warning("Catalyst storage %s failed: %s", what, err)


def _summary_row(run_id, origin, result):
    s = result["summary"]
    return {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "origin": origin[:200],
        "window_start": s["window_start"],
        "n_hours": s["n_hours"],
        "peak_demand_kw": s["peak_demand_kw"],
        "renewable_hours": s["renewable_hours"],
        "overload_hours": s["overload_hours"],
        "cost_saving": s["battery"]["cost_saving"],
        "model_source": result["model"]["source"],
    }


def save_run(origin, result):
    run_id = uuid.uuid4().hex[:12]
    row = _summary_row(run_id, origin, result)
    with _lock:
        _memory[run_id] = {"summary": row, "result": result}
        while len(_memory) > MEMORY_LIMIT:
            _memory.popitem(last=False)
    if USE_CATALYST:
        try:
            app = _catalyst_app()
            app.datastore().table(TABLE).insert_row(row)
            if BUCKET:
                app.stratus().bucket(BUCKET).put_object(
                    f"runs/{run_id}.json", json.dumps(result), {"content_type": "application/json", "overwrite": "true"}
                )
        except Exception as e:  # storage must never break a forecast
            _note_failure("save_run", e)
    return run_id


def save_upload(filename, text):
    if not (USE_CATALYST and BUCKET):
        return
    try:
        safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:80] or "upload.csv"
        key = f"uploads/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{safe}"
        _catalyst_app().stratus().bucket(BUCKET).put_object(key, text, {"content_type": "text/csv", "overwrite": "true"})
    except Exception as e:
        _note_failure("save_upload", e)


def list_runs(limit=20):
    if USE_CATALYST:
        try:
            rows = _catalyst_app().zcql().execute_query(
                f"SELECT * FROM {TABLE} ORDER BY CREATEDTIME DESC LIMIT {int(limit)}"
            )
            return [{k: v for k, v in r[TABLE].items() if not k.isupper()} for r in rows]
        except Exception as e:
            _note_failure("list_runs", e)
    with _lock:
        return [v["summary"] for v in reversed(_memory.values())][:limit]


def get_run(run_id):
    with _lock:
        if run_id in _memory:
            return _memory[run_id]["result"]
    if USE_CATALYST and BUCKET:
        try:
            obj = _catalyst_app().stratus().bucket(BUCKET).get_object(f"runs/{run_id}.json")
            data = obj.read() if hasattr(obj, "read") else obj
            return json.loads(data)
        except Exception as e:
            _note_failure("get_run", e)
    return None
