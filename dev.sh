#!/usr/bin/env bash
# Dev: bind-mount source + uvicorn --reload on backend/*.py changes.
# Stops a running ./deploy.sh stack first if present.
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_DEPLOY=(docker compose -f docker-compose.yaml)
COMPOSE_DEV=(docker compose -f docker-compose.yaml -f docker-compose.dev.yaml)

if [[ ! -f .env ]] && [[ -f .env.example ]]; then
  cp .env.example .env
fi

# shellcheck disable=SC1091
source .env 2>/dev/null || true
CONTAINER_NAME="${VAD_CONTAINER_NAME:-vad-service}"

export VAD_RELOAD=true
export WATCHFILES_FORCE_POLLING=true

container_running() {
  docker ps -q -f "name=^${CONTAINER_NAME}$" 2>/dev/null | grep -q .
}

is_dev_stack() {
  docker inspect "${CONTAINER_NAME}" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null \
    | grep -qx '/app/backend'
}

if container_running && ! is_dev_stack; then
  echo "VAD deploy stack is running — stopping it before dev..."
  "${COMPOSE_DEPLOY[@]}" down
elif container_running && is_dev_stack; then
  "${COMPOSE_DEV[@]}" down 2>/dev/null || true
fi

echo "VAD dev mode: bind-mounted backend/frontend + reload on *.py changes"
exec "${COMPOSE_DEV[@]}" up -d --build "$@"
