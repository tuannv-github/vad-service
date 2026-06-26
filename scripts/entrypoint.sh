#!/usr/bin/env bash
set -euo pipefail

cd /app/backend

reload_enabled=false
case "${VAD_RELOAD:-false}" in
  1|true|TRUE|yes|YES) reload_enabled=true ;;
esac

if [[ "${reload_enabled}" == "true" ]]; then
  export WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}"
  echo "VAD dev reload: watching /app/backend/*.py (polling=${WATCHFILES_FORCE_POLLING})"
  exec uvicorn main:app \
    --host "${VAD_HOST:-0.0.0.0}" \
    --port "${VAD_PORT:-8080}" \
    --reload \
    --reload-dir /app/backend \
    --reload-include '*.py' \
    --reload-delay "${VAD_RELOAD_DELAY:-0.5}" \
    --log-level info
fi

exec python main.py
