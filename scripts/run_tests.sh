#!/usr/bin/env bash
# Offline test suite — no browser required.
# Uses the newest installed python3.x; override with PYTHON=<path>.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=py.sh
source "$DIR/py.sh"
PY="$(pick_python)"
echo "Using: $PY ($(command -v "$PY" || echo unknown))"
cd "$DIR/.."
"$PY" -m unittest discover -s tests -v
