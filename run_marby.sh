#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export DMARBY_SMTP_APP_PASSWORD="$(cat "$HOME/.config/dmarby/smtp_password")"
exec .venv/bin/python dmarby.py --hardware "$@" >> marby.log 2>&1
