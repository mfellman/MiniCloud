#!/usr/bin/env bash
# Local pytest: all services + orchestration (workflow `minimal` via ASGI).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r tests/requirements.txt
exec .venv/bin/pytest tests/ "$@"
