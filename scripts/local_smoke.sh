#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export BROWSER_URL="${BROWSER_URL:-http://127.0.0.1:8000}"
export PUBLIC_APP_URL="$BROWSER_URL"
port=$(.venv/bin/python - <<'PY'
from urllib.parse import urlsplit
from bridge.config import BROWSER_URL

origin = urlsplit(BROWSER_URL)
if (
    origin.scheme != "http"
    or origin.hostname not in {"127.0.0.1", "localhost"}
    or origin.username is not None
    or origin.path
    or origin.query
    or origin.fragment
    or not origin.port
):
    raise SystemExit("Use a loopback HTTP BROWSER_URL with an explicit port for local smoke")
print(origin.port)
PY
)
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m bridge.seed
.venv/bin/python -m uvicorn bridge.main:app --host 127.0.0.1 --port "$port" >local-api.log 2>&1 &
api=$!
cleanup() {
  kill "$api" ${worker:+"$worker"} 2>/dev/null || true
  wait "$api" ${worker:+"$worker"} 2>/dev/null || true
}
trap cleanup EXIT
for _ in {1..60}; do
  if ! kill -0 "$api" 2>/dev/null; then
    break
  fi
  if [[ -f local-api.log && $(<local-api.log) == *"Uvicorn running on"* ]] &&
    curl --noproxy localhost,127.0.0.1 -fsS "$BROWSER_URL/health/ready" >/dev/null 2>&1; then
    .venv/bin/python -m bridge.worker >local-worker.log 2>&1 &
    worker=$!
    .venv/bin/python -m scripts.run_smoke
    exit 0
  fi
  sleep .25
done
echo "API did not start. See local-api.log" >&2
exit 1
