# Setup: QuickML model → AppSail deploy → submission

Steps marked **(console)** happen in the Catalyst web console. The rest are terminal commands from the repo root.

## 0. Prerequisites

- A Catalyst account (free) at https://console.catalyst.zoho.in (India data center) and a project, e.g. `gridmind`. All defaults in this repo already use the India hosts (`api.catalyst.zoho.in`, `accounts.zoho.in`, `api-console.zoho.in`).
- Catalyst CLI: `npm install -g zcatalyst-cli`, then `catalyst login`.
- **(console)** Open the project → **QuickML** → **Start Exploring** (one-time activation).

## 1. Train the model in QuickML (console)

1. **Datasets → Import → Local file:** upload `quickml/quickml_train.csv` (4,000 rows, 11 features + `target_demand_kw`).
   - Upload this file, **not** the raw `train_telemetry.csv`. The engineered columns are what make the model accurate.
2. **Pipelines → Create → Prediction pipeline → Regression**, using that dataset, with target `target_demand_kw`.
3. In the builder, connect the source straight to the **Linear Regression** algorithm stage.
   - Do not add feature-selection or PCA stages. They can drop the interaction columns.
   - Tick **Generate Model Explainer** so the dashboard can show QuickML SHAP values.
4. Run the pipeline. Expect test-split RMSE of roughly 8.5–9.5 kW.
   - Optional: also run an AutoML regression pipeline on the same dataset and keep whichever scores better. In local tests linear beat every tree ensemble on future data.
5. **Models → your model → Create endpoint → Publish.** From the endpoint page, copy the **endpoint key**, then note the OAuth scope and the sample request in **Connection details**.
6. Find your **project ID** (Project settings, or the console URL) and your **org ID** (Project settings → General).

## 2. OAuth credentials (for the app to call QuickML)

1. Go to https://api-console.zoho.in (or `.com` etc.) → **Add Client → Self Client**. Copy the client ID and secret.
2. Open the **Generate Code** tab. Use scope `QuickML.deployment.READ`, duration 10 min, then click Create and copy the code.
3. Run, within 10 minutes:
   ```bash
   python3 scripts/get_refresh_token.py --client-id <id> --client-secret <secret> --code <code> --accounts-url https://accounts.zoho.in
   ```
4. Copy `.env.example` to `.env` and fill everything in.
5. Verify the endpoint end to end:
   ```bash
   set -a; source .env; set +a
   .venv/bin/python scripts/check_quickml.py
   ```
   You want `PASS: QuickML matches the expected model`. If the gap is large, the pipeline differs from step 1.3.

## 3. Optional persistence (console)

- **Data Store → Create table** `ForecastRuns` with these Text/Number columns: `run_id`, `created_at`, `origin`, `window_start`, `n_hours`, `peak_demand_kw`, `renewable_hours`, `overload_hours`, `cost_saving`, `model_source`.
- **Stratus → Create bucket**, e.g. `gridmind-runs`.
- Set `GRIDMIND_STORAGE=catalyst` and `GRIDMIND_BUCKET=<bucket>`.
- If either step is skipped, the app keeps working with in-memory history and shows `storage: memory` in the header.

## 4. Deploy to AppSail

```bash
npm install -g --prefix ~/.npm-global zcatalyst-cli    # no sudo; add ~/.npm-global/bin to PATH
catalyst login
catalyst init --project <project_id> --org <org_id> -ni
scripts/build_appsail.sh      # vendors Flask etc. and writes build/app-config.json from .env
catalyst appsail:add --name gridmind --stack python_3_12 --source "$PWD/appsail/build" --build ./ --command "python3 app.py" -ni
catalyst deploy --only appsail:gridmind -ni
```

- `appsail/build/` is gitignored, so credentials reach AppSail without ever being in a committed file. The committed `appsail/app-config.json` keeps empty placeholders.
- The refresh token, not `ZOHO_ACCESS_TOKEN`, is shipped, so the app renews its own access tokens.
- Redeploying: `scripts/build_appsail.sh && catalyst deploy --only appsail:gridmind -ni`. A deploy **replaces** env vars set in the console, so change them in `.env` instead.
- Open the URL the CLI prints. The header badge should read **● QuickML live**.

## 5. Point predict.py at the app and self-test

```bash
export GRIDMIND_PREDICT_URL=<AppSail URL from the deploy output>/api/predict
python predict.py --input sample_test_input.csv --output predictions.csv   # should print "from QuickML via …"
python test_eval.py                                                          # must print SUCCESS
```

Then hard-code that URL as the default for `QUICKML_ENDPOINT_URL` in `predict.py`, because evaluators won't set env vars.

## 6. Submission checklist

- [x] Public AppSail URL loads and shows **QuickML live**
- [x] `predict.py` default URL set; `python test_eval.py` prints SUCCESS on a clean machine
- [x] `scripts/check_quickml.py` passes
- [x] README live-app link filled in
- [ ] No secrets in the repo (`git grep -i refresh_token` shows only placeholders)
- [ ] GitHub repo public, or `TeamName_Submission.zip`
