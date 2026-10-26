"""Build the QuickML training set and validate the modelling approach.

Run:  .venv/bin/python ml/train.py

Outputs
  quickml/quickml_train.csv        upload this to QuickML (target: target_demand_kw)
  appsail/gridcore/model_params.json
      hour_base_load   lookup used by features.py
      fallback_model   linear coefficients mirroring the QuickML model, used only
                       when the QuickML endpoint is unreachable
      residuals        holdout error quantiles for prediction intervals
  ml/model_report.json             validation scores for the README / dashboard
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "appsail"))
from gridcore import features as fx  # noqa: E402

TARGET = "target_demand_kw"
FOLD_STARTS = [2400, 2800, 3200, 3600]  # rolling-origin folds, 400 hours each
FOLD_LEN = 400


def hour_profile(df):
    """Mean demand per hour on mild days (no heating/cooling load)."""
    mild = df[(df.ambient_temp_c < fx.COOLING_THRESHOLD_C) & (df.ambient_temp_c > fx.HEATING_THRESHOLD_C)]
    prof = mild.groupby(pd.to_datetime(mild.timestamp).dt.hour)[TARGET].mean()
    return {str(h): round(float(prof[h]), 3) for h in range(24)}


def feature_frame(df, params):
    rows = [fx.build_features(r, params) for r in df[fx.RAW_COLUMNS].to_dict("records")]
    return pd.DataFrame(rows, columns=fx.FEATURE_COLUMNS)


def rmse(a, b):
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def main():
    df = pd.read_csv(os.path.join(ROOT, "train_telemetry.csv"))
    y = df[TARGET].to_numpy()

    candidates = {
        "linear (engineered)": lambda: LinearRegression(),
        "gradient boosting (engineered)": lambda: GradientBoostingRegressor(n_estimators=400, max_depth=3, learning_rate=0.03, random_state=0),
        "random forest (engineered)": lambda: RandomForestRegressor(300, min_samples_leaf=5, random_state=0, n_jobs=-1),
        "gradient boosting (raw 4 cols)": "raw",
        "default boosting (raw 4 cols)": "raw-default",
        "starter-kit dummy formula": "dummy",
    }
    hours_all = pd.to_datetime(df.timestamp).dt.hour
    raw = lambda d: pd.DataFrame({"hour": pd.to_datetime(d.timestamp).dt.hour, "t": d.ambient_temp_c, "h": d.humidity_pct, "s": d.solar_irradiance_wm2})

    scores = {name: [] for name in candidates}
    holdout_residuals = []
    for start in FOLD_STARTS:
        train, test = df.iloc[:start], df.iloc[start:start + FOLD_LEN]
        # Profile is re-learned per fold so validation never sees future targets.
        params = {"hour_base_load": hour_profile(train)}
        Xtr, Xte = feature_frame(train, params), feature_frame(test, params)
        for name, make in candidates.items():
            if make == "raw":
                m = GradientBoostingRegressor(n_estimators=400, max_depth=3, learning_rate=0.03, random_state=0)
                pred = m.fit(raw(train), y[:start]).predict(raw(test))
            elif make == "raw-default":
                pred = HistGradientBoostingRegressor(random_state=0).fit(raw(train), y[:start]).predict(raw(test))
            elif make == "dummy":
                # The fallback formula shipped in the starter predict.py.
                h = hours_all.iloc[start:start + FOLD_LEN]
                pred = (120 + 30 * np.sin(2 * np.pi * (h - 6) / 24) + np.maximum(0, test.ambient_temp_c - 24) * 8.5).to_numpy()
            else:
                pred = make().fit(Xtr, y[:start]).predict(Xte)
            scores[name].append(rmse(pred, y[start:start + FOLD_LEN]))
            if name == "linear (engineered)":
                holdout_residuals.extend((y[start:start + FOLD_LEN] - pred).tolist())

    print("Rolling-origin CV RMSE (kW), folds of 400 hours:")
    for name, s in scores.items():
        print(f"  {name:34s} mean {np.mean(s):6.2f}   folds {[round(v, 2) for v in s]}")

    # Final artefacts, fitted on all training data.
    params = {"hour_base_load": hour_profile(df)}
    hours = pd.to_datetime(df.timestamp).dt.hour
    # Per-hour weather means: last-resort fill when a whole column is missing.
    params["climatology"] = {
        col: {str(h): round(float(v), 2) for h, v in df.groupby(hours)[col].mean().items()}
        for col in fx.RAW_COLUMNS[1:]
    }
    X = feature_frame(df, params)
    model = LinearRegression().fit(X, y)
    in_sample = rmse(model.predict(X), y)

    res = np.array(holdout_residuals)
    params["fallback_model"] = {
        "intercept": round(float(model.intercept_), 6),
        "coefficients": {c: round(float(w), 6) for c, w in zip(fx.FEATURE_COLUMNS, model.coef_)},
    }
    params["residuals"] = {
        "rmse": round(float(np.sqrt(np.mean(res ** 2))), 3),
        "q05": round(float(np.quantile(res, 0.05)), 3),
        "q95": round(float(np.quantile(res, 0.95)), 3),
        # Sorted sample of holdout errors: lets the backend estimate
        # P(actual demand >= threshold) empirically without numpy.
        "sample": sorted(round(float(v), 2) for v in res[:: max(1, len(res) // 400)]),
    }
    with open(fx.PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=1)

    out = X.copy()
    out[TARGET] = y
    os.makedirs(os.path.join(ROOT, "quickml"), exist_ok=True)
    out.to_csv(os.path.join(ROOT, "quickml", "quickml_train.csv"), index=False)

    report = {
        "cv_rmse": {k: {"mean": round(float(np.mean(v)), 3), "folds": [round(x, 3) for x in v]} for k, v in scores.items()},
        "final_in_sample_rmse": round(in_sample, 3),
        "interval_90": [params["residuals"]["q05"], params["residuals"]["q95"]],
        "n_train_rows": int(len(df)),
        "features": fx.FEATURE_COLUMNS,
    }
    with open(os.path.join(ROOT, "ml", "model_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nFinal linear model in-sample RMSE {in_sample:.2f} kW")
    print(f"90% prediction interval: [{params['residuals']['q05']}, +{params['residuals']['q95']}] kW around the forecast")
    print("Wrote quickml/quickml_train.csv, appsail/gridcore/model_params.json, ml/model_report.json")


if __name__ == "__main__":
    main()
