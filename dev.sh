#!/usr/bin/env bash
# Dev mode: volume mounts + uvicorn auto-reload on backend/*.py changes.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

export VAD_RELOAD=true
export WATCHFILES_FORCE_POLLING=true

echo "VAD dev mode: backend/*.py changes trigger server reload"
exec docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up --build "$@"
