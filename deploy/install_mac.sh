#!/usr/bin/env zsh
# Development install on macOS (zsh). Creates .venv with Python 3.11/3.12, installs deps,
# prepares .env/data/logs and validates the configuration.
#   ./deploy/install_mac.sh            # runtime + dev/test dependencies
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=""
for candidate in python3.11 python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.12 \
                 /usr/local/bin/python3.11 /usr/local/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$(command -v "$candidate")"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.11 or 3.12 not found. Install with:  brew install python@3.11" >&2
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements-dev.txt

mkdir -p data logs certs
chmod 700 data logs certs
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example — edit it and set real credentials."
fi

echo "\nValidating configuration (no PLC connections are opened):"
.venv/bin/python scripts/validate_config.py --config-dir config || true
.venv/bin/python scripts/validate_config.py --config-dir config/sim >/dev/null && echo "config/sim: VALID"

cat <<EOF

Next steps
  1. Edit .env (MySQL credentials).            source .venv/bin/activate
  2. Create schema:     python scripts/init_database.py
  3. Simulators (dev):  python -m simulators
  4. Run against sims:  python main.py --config-dir config/sim
  5. Tests:             pytest                    (MySQL tests: MYSQL_TEST_DSN=mysql://... pytest -m mysql)
EOF
