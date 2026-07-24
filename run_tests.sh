#!/usr/bin/env bash
# Offline test suite — no browser required.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
python3.14 -m unittest discover -s tests -v
