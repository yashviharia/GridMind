"""Verify the published QuickML endpoint before submitting.

    .venv/bin/python scripts/check_quickml.py

1. Calls QuickML with the last 400 training hours (a summer holdout it was
   trained on, so this checks wiring, not generalisation) and reports RMSE.
2. Compares every QuickML prediction with the local mirror model: a large gap
   means the QuickML pipeline was built differently (wrong algorithm, a column
   dropped, or an extra normalisation stage).
3. Reports whether the endpoint accepts batched requests.
"""
import csv
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "appsail"))
from gridcore.features import build_features  # noqa: E402
from gridcore.forecast import local_predict  # noqa: E402
from gridcore.quickml_client import QuickMLClient, load_env_file  # noqa: E402


def main():
    load_env_file(os.path.join(ROOT, ".env"))
    client = QuickMLClient()
    if not client.configured:
        raise SystemExit("Missing settings: " + ", ".join(client.missing_settings()))

    with open(os.path.join(ROOT, "train_telemetry.csv")) as f:
        rows = list(csv.DictReader(f))[-400:]
    feats = [build_features(r) for r in rows]
    actual = [float(r["target_demand_kw"]) for r in rows]

    t0 = time.time()
    single = client.predict_one(feats[0])
    print(f"Single call OK: {single:.2f} kW ({1000 * (time.time() - t0):.0f} ms)")

    t0 = time.time()
    preds = client.predict_many(feats)
    print(f"{len(preds)} predictions in {time.time() - t0:.1f} s; batched requests supported: {client._batch_supported}")

    mirror = [local_predict(f) for f in feats]
    rmse = math.sqrt(sum((p - a) ** 2 for p, a in zip(preds, actual)) / len(actual))
    rmse_mirror = math.sqrt(sum((p - a) ** 2 for p, a in zip(mirror, actual)) / len(actual))
    gap = max(abs(p - m) for p, m in zip(preds, mirror))
    print(f"RMSE QuickML {rmse:.2f} kW | local mirror {rmse_mirror:.2f} kW | max gap {gap:.2f} kW")
    if gap > 5:
        print("WARNING: QuickML differs noticeably from the mirror. Check the pipeline matches QUICKML_SETUP.md.")
    else:
        print("PASS: QuickML matches the expected model.")


if __name__ == "__main__":
    main()
