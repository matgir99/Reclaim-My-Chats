#!/usr/bin/env bash
# Offline test suite — no browser required.
# Set PYTHON to override the interpreter (e.g. PYTHON=python3.14).
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m unittest discover -s tests -v
