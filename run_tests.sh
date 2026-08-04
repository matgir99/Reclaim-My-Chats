#!/usr/bin/env bash
# Offline test suite — no browser required.
# Uses the newest installed python3.x; override with PYTHON=<path>.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/py.sh
source "$DIR/scripts/py.sh"
PY="$(pick_python)"
echo "Using: $PY ($(command -v "$PY" || echo unknown))"
"$PY" -m unittest discover -s tests -v
