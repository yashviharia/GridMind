"""HackSphere 3.0 prediction script: GridMind demand forecaster.

    python predict.py --input sample_test_input.csv --output predictions.csv

predict(input_df) returns one demand forecast (kW) per input row, in order.
Features are built with the exact code used to create the QuickML training
set (appsail/gridcore/features.py), then sent to the published QuickML model.

Where predictions come from, in order:
  1. QuickML REST endpoint directly, if QuickML credentials are in the
     environment (see README: QUICKML_PROJECT_ID, QUICKML_ENDPOINT_KEY,
     ZOHO_ORG_ID, and ZOHO_ACCESS_TOKEN or ZOHO_CLIENT_ID/SECRET/REFRESH_TOKEN;
     read from .env automatically when present).
  2. The GridMind AppSail API (/api/predict), which holds the QuickML
     credentials server-side and calls the same QuickML endpoint. This is what
     runs for evaluators, so no secrets need to live in this file.
  3. Local mirror of the QuickML model (same features, same linear form) only
     if both are unreachable, with a loud warning, so evaluation never crashes.
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "appsail"))

from gridcore.cleaning import clean_rows  # noqa: E402
from gridcore.features import RAW_COLUMNS, build_features, load_params  # noqa: E402
from gridcore.forecast import local_predict  # noqa: E402
from gridcore.quickml_client import QuickMLClient, QuickMLError, load_env_file  # noqa: E402

# Local runs pick up QuickML credentials from .env; evaluators without one use the API below.
load_env_file(os.path.join(ROOT, ".env"))

# Public URL of the deployed GridMind app's prediction API (proxies QuickML).
QUICKML_ENDPOINT_URL = os.environ.get(
    "GRIDMIND_PREDICT_URL", "https://gridmind-50047185567.development.catalystappsail.in/api/predict"
)
CHUNK_ROWS = 100
TIMEOUT_S = 120


def _records(input_df):
    df = input_df.copy()
    df["timestamp"] = df["timestamp"].astype(str)
    cols = [c for c in RAW_COLUMNS if c in df.columns]
    # NaN -> None so missing values reach the cleaner (it imputes them).
    return df[cols].astype(object).where(df[cols].notna(), None).to_dict(orient="records")


def _via_appsail(records):
    preds = []
    for i in range(0, len(records), CHUNK_ROWS):
        chunk = records[i:i + CHUNK_ROWS]
        resp = requests.post(QUICKML_ENDPOINT_URL, json={"data": chunk}, timeout=TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()
        if len(body["predictions"]) != len(chunk):
            raise RuntimeError(f"expected {len(chunk)} predictions, got {len(body['predictions'])}")
        preds.extend(body["predictions"])
    return preds


def predict(input_df: pd.DataFrame):
    """Parameters:
        input_df (pandas.DataFrame): DataFrame with columns
        ['timestamp', 'ambient_temp_c', 'humidity_pct', 'solar_irradiance_wm2']

    Returns:
        numpy.ndarray: Predicted demand (kW) for each input row.
    """
    records = _records(input_df)
    client = QuickMLClient()

    if client.configured:
        try:
            rows, _ = clean_rows(records, preserve_rows=True)
            params = load_params()
            preds = client.predict_many([build_features(r, params) for r in rows])
            print(f"[predict] {len(preds)} predictions from QuickML (direct).")
            return np.clip(np.array(preds, dtype=float), 0, None)
        except QuickMLError as e:
            print(f"Warning: direct QuickML call failed ({e}); trying the GridMind API.")

    if QUICKML_ENDPOINT_URL.startswith("http"):
        try:
            preds = _via_appsail(records)
            print(f"[predict] {len(preds)} predictions from QuickML via {QUICKML_ENDPOINT_URL}.")
            return np.clip(np.array(preds, dtype=float), 0, None)
        except Exception as e:
            print(f"Warning: GridMind API call failed ({e}).")

    print("WARNING: QuickML unreachable. Using the local mirror of the QuickML model.")
    rows, _ = clean_rows(records, preserve_rows=True)
    params = load_params()
    preds = [local_predict(build_features(r, params)) for r in rows]
    return np.clip(np.array(preds, dtype=float), 0, None)


def main():
    parser = argparse.ArgumentParser(description="GridMind demand forecaster")
    parser.add_argument("--input", default="sample_test_input.csv")
    parser.add_argument("--output", default="predictions.csv")
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    preds = predict(df)
    out = pd.DataFrame({"timestamp": df["timestamp"], "target_demand_kw": np.round(preds, 2)})
    out.to_csv(args.output, index=False)
    print(f"Wrote {len(out)} predictions to {args.output}")
    print(out.head().to_string(index=False))


if __name__ == "__main__":
    main()
