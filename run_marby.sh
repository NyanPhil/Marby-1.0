#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec .venv/bin/python dmarby.py --hardware "$@" >> marby.log 2>&1
