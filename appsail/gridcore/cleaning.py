"""Validation and cleanup of raw telemetry before it reaches the model.

Every fix is recorded as an issue so the dashboard can show exactly what was
changed and why, instead of silently massaging the data.
"""
from datetime import datetime, timedelta

from .features import TIMESTAMP_FORMAT, load_params

NUMERIC_COLUMNS = ["ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]

# Physically plausible ranges; values outside are treated as sensor faults.
VALID_RANGE = {
    "ambient_temp_c": (-40.0, 60.0),
    "humidity_pct": (0.0, 100.0),
    "solar_irradiance_wm2": (0.0, 1400.0),
}

_TS_FORMATS = [
    TIMESTAMP_FORMAT,
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
]

# Column-name aliases accepted from uploads, so small header typos don't fail a demo.
_ALIASES = {
    "time": "timestamp", "datetime": "timestamp", "date_time": "timestamp",
    "temp": "ambient_temp_c", "temperature": "ambient_temp_c", "ambient_temp": "ambient_temp_c",
    "humidity": "humidity_pct", "rh": "humidity_pct",
    "solar": "solar_irradiance_wm2", "irradiance": "solar_irradiance_wm2", "solar_irradiance": "solar_irradiance_wm2",
}


class CleaningError(ValueError):
    pass


def _parse_ts(value):
    text = str(value).strip().replace("Z", "")
    if "+" in text[10:]:
        text = text[: 10 + text[10:].index("+")]
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)  # NaN check
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "null", "none", "na", "n/a", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalise_keys(row):
    out = {}
    for k, v in row.items():
        key = str(k).strip().lower().replace(" ", "_")
        out[_ALIASES.get(key, key)] = v
    return out


def clean_rows(raw_rows, preserve_rows=False):
    """Validate and repair telemetry rows.

    preserve_rows=True keeps one output row per input row in the original order
    (required by the predict.py contract); otherwise bad timestamps are dropped,
    duplicates collapsed and rows sorted by time.

    Returns (rows, issues) where rows have canonical keys and numeric values.
    """
    if not raw_rows:
        raise CleaningError("No telemetry rows supplied.")

    issues = []
    rows = []
    for i, raw in enumerate(raw_rows):
        r = normalise_keys(raw)
        if "timestamp" not in r:
            raise CleaningError("Missing required column 'timestamp'.")
        ts = _parse_ts(r.get("timestamp"))
        if ts is None:
            if preserve_rows:
                raise CleaningError(f"Row {i + 1}: unparseable timestamp {r.get('timestamp')!r}.")
            issues.append({"row": i + 1, "field": "timestamp", "action": "dropped", "detail": f"unparseable value {r.get('timestamp')!r}"})
            continue
        clean = {"timestamp": ts, "_original_ts": r.get("timestamp"), "_row": i + 1}
        for col in NUMERIC_COLUMNS:
            val = _to_float(r.get(col))
            lo, hi = VALID_RANGE[col]
            if val is not None and not (lo <= val <= hi):
                # Small humidity/solar overshoots are sensor saturation: clip them.
                if col != "ambient_temp_c" and lo - 5 <= val <= hi * 1.05:
                    issues.append({"row": i + 1, "field": col, "action": "clipped", "detail": f"{val} -> {min(max(val, lo), hi)}"})
                    val = min(max(val, lo), hi)
                else:
                    issues.append({"row": i + 1, "field": col, "action": "rejected", "detail": f"{val} outside {lo}..{hi}"})
                    val = None
            clean[col] = val
        rows.append(clean)

    if not rows:
        raise CleaningError("No rows with a valid timestamp.")

    if not preserve_rows:
        by_ts = {}
        for r in rows:
            if r["timestamp"] in by_ts:
                issues.append({"row": None, "field": "timestamp", "action": "deduplicated", "detail": r["timestamp"].strftime(TIMESTAMP_FORMAT)})
            by_ts[r["timestamp"]] = r
        rows = _fill_gaps([by_ts[k] for k in sorted(by_ts)], issues)

    _impute(rows, issues, ordered=not preserve_rows)

    for r in rows:
        if preserve_rows and r["_original_ts"] is not None:
            # Echo the caller's timestamp string back untouched.
            r["timestamp_str"] = str(r["_original_ts"])
        else:
            r["timestamp_str"] = r["timestamp"].strftime(TIMESTAMP_FORMAT)
        del r["_original_ts"], r["_row"]
    return rows, issues


MAX_FILL_GAP_H = 6


def _fill_gaps(rows, issues):
    """Insert rows for short gaps in the hourly series (values imputed later);
    longer gaps are reported but left alone rather than invented."""
    out = [rows[0]]
    for cur in rows[1:]:
        prev = out[-1]
        gap_h = int(round((cur["timestamp"] - prev["timestamp"]).total_seconds() / 3600))
        if gap_h > 1:
            stamp = prev["timestamp"].strftime(TIMESTAMP_FORMAT)
            if gap_h - 1 <= MAX_FILL_GAP_H:
                for k in range(1, gap_h):
                    out.append({"timestamp": prev["timestamp"] + timedelta(hours=k), "_original_ts": None, "_row": None,
                                **{c: None for c in NUMERIC_COLUMNS}})
                issues.append({"row": None, "field": "timestamp", "action": "gap filled", "detail": f"{gap_h - 1} missing hour(s) after {stamp} inserted"})
            else:
                issues.append({"row": None, "field": "timestamp", "action": "gap", "detail": f"{gap_h - 1} missing hours after {stamp} (too long to fill)"})
        out.append(cur)
    return out


def _impute(rows, issues, ordered):
    """Fill missing values: linear interpolation in time, then edge-fill, then
    the training-set hourly average if a column has no usable values at all."""
    climatology = load_params()["climatology"]
    order = sorted(range(len(rows)), key=lambda i: rows[i]["timestamp"]) if not ordered else list(range(len(rows)))
    for col in NUMERIC_COLUMNS:
        known = [i for i in order if rows[i][col] is not None]
        for pos, idx in enumerate(order):
            if rows[idx][col] is not None:
                continue
            before = next((j for j in reversed(order[:pos]) if rows[j][col] is not None), None)
            after = next((j for j in order[pos + 1:] if rows[j][col] is not None), None)
            if before is not None and after is not None:
                t0, t1, t = (rows[k]["timestamp"].timestamp() for k in (before, after, idx))
                w = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                val, how = rows[before][col] + w * (rows[after][col] - rows[before][col]), "interpolated"
            elif known:
                val, how = rows[before if before is not None else after][col], "edge-filled"
            else:
                val, how = climatology[col][str(rows[idx]["timestamp"].hour)], "hourly average"
            rows[idx][col] = round(val, 3)
            issues.append({"row": rows[idx]["_row"], "field": col, "action": "imputed", "detail": f"{how} -> {rows[idx][col]}"})
