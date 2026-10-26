#!/usr/bin/env bash
# Assemble appsail/build/: app code + Linux wheels for the AppSail python_3_12 runtime,
# plus an app-config.json whose env_variables come from .env (build/ is gitignored,
# so real credentials never land in a committed file).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/appsail"
OUT="$SRC/build"
rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$SRC/app.py" "$SRC/gridcore" "$SRC/static" "$SRC/data" "$SRC/requirements.txt" "$OUT/"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +
python3 -m pip install -r "$SRC/requirements.txt" -t "$OUT" \
  --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --only-binary=:all: --quiet

python3 - "$SRC/app-config.json" "$ROOT/.env" "$OUT/app-config.json" <<'PY'
import json, os, sys
template, env_path, out = sys.argv[1:]
cfg = json.load(open(template))
cfg["build_path"] = "./"  # the build folder itself is the deploy source
env = {}
if os.path.exists(env_path):
    for line in open(env_path):
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#") and v:
            env[k] = v
# Only keys the template declares; ZOHO_ACCESS_TOKEN (1 hour) is never shipped.
for k in cfg["env_variables"]:
    if env.get(k):
        cfg["env_variables"][k] = env[k]
missing = [k for k, v in cfg["env_variables"].items() if not v and k != "GRIDMIND_BUCKET"]
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
print("app-config.json written" + (f"; still empty: {', '.join(missing)}" if missing else " with all QuickML settings"))
PY
echo "Built $OUT ($(du -sh "$OUT" | cut -f1))"
