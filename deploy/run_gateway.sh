#!/usr/bin/env zsh
# Run the gateway in the foreground (macOS/Linux). Extra arguments are passed to main.py, e.g.
#   ./deploy/run_gateway.sh --config-dir config/sim
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/python ]]; then
  echo "virtualenv missing: run ./deploy/install_mac.sh first" >&2
  exit 1
fi
exec .venv/bin/python main.py "$@"
