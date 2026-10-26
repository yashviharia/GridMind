"""Exchange a Zoho Self Client grant code for OAuth tokens.

1. https://api-console.zoho.in  (use your data center's domain) -> Add Client -> Self Client
2. Generate Code tab: scope  QuickML.deployment.READ , duration 10 minutes
3. Within 10 minutes run:
     python scripts/get_refresh_token.py --code ... --write-env
   (client ID/secret come from .env, or pass --client-id/--client-secret)

With --write-env the client ID/secret, refresh token and a fresh access token
(valid 1 hour) are saved into .env and nothing secret is printed. Without it
the tokens are printed. Stdlib only.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "appsail"))
from gridcore.quickml_client import ssl_context  # noqa: E402

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def write_env(values, path=ENV_PATH):
    """Set KEY=value lines in .env in place, appending keys that are missing."""
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def _env(key):
    """Read a value from .env without exporting anything."""
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            k, _, v = line.strip().partition("=")
            if k == key and v:
                return v
    return os.environ.get(key, "")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--client-id", default=_env("ZOHO_CLIENT_ID"), help="defaults to ZOHO_CLIENT_ID in .env")
    p.add_argument("--client-secret", default=_env("ZOHO_CLIENT_SECRET"), help="defaults to ZOHO_CLIENT_SECRET in .env")
    p.add_argument("--code", required=True, help="grant code from the Self Client 'Generate Code' tab")
    p.add_argument("--accounts-url", default="https://accounts.zoho.in", help="e.g. https://accounts.zoho.com for the US DC")
    p.add_argument("--write-env", action="store_true", help="save the tokens into .env instead of printing them")
    a = p.parse_args()
    if not (a.client_id and a.client_secret):
        p.error("pass --client-id/--client-secret or set ZOHO_CLIENT_ID/ZOHO_CLIENT_SECRET in .env")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": a.client_id,
        "client_secret": a.client_secret,
        "code": a.code,
    }).encode()
    req = urllib.request.Request(f"{a.accounts_url.rstrip('/')}/oauth/v2/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as resp:
        payload = json.loads(resp.read())
    if "refresh_token" not in payload:
        raise SystemExit(f"No refresh token returned: {payload.get('error', payload)}. "
                         "Grant codes expire after the chosen duration and work only once; generate a new one.")
    print(f"Granted scope: {payload.get('scope', '?')}")

    if a.write_env:
        write_env({
            "ZOHO_CLIENT_ID": a.client_id,
            "ZOHO_CLIENT_SECRET": a.client_secret,
            "ZOHO_REFRESH_TOKEN": payload["refresh_token"],
            "ZOHO_ACCESS_TOKEN": payload.get("access_token", ""),
        })
        print(f"Saved client ID/secret, refresh token and access token to {ENV_PATH}")
    else:
        print("ZOHO_REFRESH_TOKEN=" + payload["refresh_token"])
        print("ZOHO_ACCESS_TOKEN=" + payload.get("access_token", ""))
    print(f"(access token valid {payload.get('expires_in', '?')} s; the refresh token renews it automatically)")


if __name__ == "__main__":
    main()
