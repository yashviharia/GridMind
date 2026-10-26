"""Client for a published Catalyst QuickML prediction endpoint (stdlib only).

REST contract (from the Catalyst QuickML docs):
  POST {api_host}/quickml/v1/project/{project_id}/endpoints/predict
  headers: X-QUICKML-ENDPOINT-KEY, Authorization: Zoho-oauthtoken <token>, CATALYST-ORG
  body:    {"data": {feature: value, ...}}
  reply:   {"result": [prediction], "status": "success"}

Access tokens are minted from a long-lived refresh token and cached until
shortly before they expire, so the app never needs a manual token paste.
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def ssl_context():
    """Default TLS context, using certifi's CA bundle when installed.

    python.org builds of Python on macOS ship without root certificates, which
    makes every HTTPS call fail with CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = ssl_context()


class QuickMLError(RuntimeError):
    pass


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file into os.environ (local runs only).

    Variables already set in the environment win, and a missing file is fine,
    so AppSail (which sets real env vars) is unaffected.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value and not os.environ.get(key.strip()):
                os.environ[key.strip()] = value


class QuickMLClient:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.api_host = env.get("QUICKML_API_HOST", "https://api.catalyst.zoho.in").rstrip("/")
        self.project_id = env.get("QUICKML_PROJECT_ID", "")
        self.endpoint_key = env.get("QUICKML_ENDPOINT_KEY", "")
        self.org_id = env.get("ZOHO_ORG_ID", "")
        self.environment = env.get("QUICKML_ENVIRONMENT", "Development")
        self.accounts_url = env.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
        self.client_id = env.get("ZOHO_CLIENT_ID", "")
        self.client_secret = env.get("ZOHO_CLIENT_SECRET", "")
        self.refresh_token = env.get("ZOHO_REFRESH_TOKEN", "")
        # A pasted access token expires in an hour, so the refresh flow wins when configured.
        has_refresh = self.client_id and self.client_secret and self.refresh_token
        self.static_token = "" if has_refresh else env.get("ZOHO_ACCESS_TOKEN", "")
        self.timeout = float(env.get("QUICKML_TIMEOUT_S", "15"))
        self._token = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        self._batch_supported = None  # learned on first multi-row call

    @property
    def configured(self):
        has_auth = bool(self.static_token or (self.client_id and self.client_secret and self.refresh_token))
        return bool(self.project_id and self.endpoint_key and self.org_id and has_auth)

    def missing_settings(self):
        names = {
            "QUICKML_PROJECT_ID": self.project_id,
            "QUICKML_ENDPOINT_KEY": self.endpoint_key,
            "ZOHO_ORG_ID": self.org_id,
        }
        missing = [k for k, v in names.items() if not v]
        if not (self.static_token or (self.client_id and self.client_secret and self.refresh_token)):
            missing.append("ZOHO_CLIENT_ID/ZOHO_CLIENT_SECRET/ZOHO_REFRESH_TOKEN")
        return missing

    @property
    def url(self):
        return f"{self.api_host}/quickml/v1/project/{self.project_id}/endpoints/predict"

    def _access_token(self):
        if self.static_token:
            return self.static_token
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            body = urllib.parse.urlencode({
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request(f"{self.accounts_url}/oauth/v2/token", data=body, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.URLError as e:
                raise QuickMLError(f"OAuth token refresh failed: {e}") from e
            if "access_token" not in payload:
                raise QuickMLError(f"OAuth token refresh rejected: {payload.get('error', payload)}")
            self._token = payload["access_token"]
            self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
            return self._token

    def _post(self, data, explain=False):
        url = self.url + ("?explainModel=true" if explain else "")
        req = urllib.request.Request(
            url,
            data=json.dumps({"data": data}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-QUICKML-ENDPOINT-KEY": self.endpoint_key,
                "Authorization": f"Zoho-oauthtoken {self._access_token()}",
                "CATALYST-ORG": self.org_id,
                "Environment": self.environment,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401 and self.static_token:
                detail += " (ZOHO_ACCESS_TOKEN lasts 1 hour; mint a new one with scripts/get_refresh_token.py)"
            raise QuickMLError(f"QuickML HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise QuickMLError(f"QuickML unreachable: {e.reason}") from e
        if payload.get("status") not in (None, "success") or "result" not in payload:
            raise QuickMLError(f"Unexpected QuickML response: {str(payload)[:300]}")
        return payload

    def predict_one(self, features):
        result = self._post(features)["result"]
        return float(result[0] if isinstance(result, list) else result)

    def predict_many(self, feature_rows, workers=8):
        """Predict a list of feature dicts, preserving order.

        Tries one batched call first (the endpoint accepts a list of records in
        some configurations); if that is rejected or returns the wrong count,
        falls back to concurrent single-row calls.
        """
        if not feature_rows:
            return []
        if len(feature_rows) > 1 and self._batch_supported is not False:
            try:
                result = self._post(feature_rows)["result"]
                if isinstance(result, list) and len(result) == len(feature_rows):
                    self._batch_supported = True
                    return [float(v) for v in result]
                self._batch_supported = False
            except QuickMLError:
                self._batch_supported = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.predict_one, feature_rows))

    def explain(self, features):
        """SHAP attributions for one row (requires the model explainer enabled)."""
        payload = self._post(features, explain=True)
        return {"prediction": float(payload["result"][0]), "explanation": payload.get("explanation")}
