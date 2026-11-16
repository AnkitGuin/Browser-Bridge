#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
if [[ -z "${BROWSER_CHROMIUM:-}" ]]; then
  .venv/bin/python -m playwright install chromium --only-shell
fi
npm --prefix console ci
npm --prefix console run build
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m bridge.seed
