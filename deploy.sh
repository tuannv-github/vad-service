#!/usr/bin/env bash
# Deploy: rebuild image and run from that image (no source bind mounts, no reload).
# Stops a running ./dev.sh stack first if present.
#
# Usage:
#   ./deploy.sh              # build + up -d
#   ./deploy.sh --down       # stop and remove (deploy or dev)
#   ./deploy.sh --restart    # recreate from a fresh image build
set -euo pipefail

cd "$(dirname "$0")"

RESTART=false
BRING_DOWN=false
COMPOSE_DEPLOY=(docker compose -f docker-compose.yaml)
COMPOSE_DEV=(docker compose -f docker-compose.yaml -f docker-compose.dev.yaml)

usage() {
  echo "Usage: $0 [--down|--restart] [-h]"
  echo "  --down       Stop and remove the container"
  echo "  --restart    Rebuild image and recreate the container"
  echo "  -h           Show this help"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      ;;
    --down|-d)
      BRING_DOWN=true
      shift
      ;;
    --restart|-r)
      RESTART=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      ;;
  esac
done

if [[ ! -f .env ]] && [[ -f .env.example ]]; then
  cp .env.example .env
fi

# shellcheck disable=SC1091
source .env 2>/dev/null || true
CONTAINER_NAME="${VAD_CONTAINER_NAME:-vad-service}"

# Deploy mode: image code, no live reload (even if .env has VAD_RELOAD=true).
export VAD_RELOAD=false
export WATCHFILES_FORCE_POLLING=false

container_running() {
  docker ps -q -f "name=^${CONTAINER_NAME}$" 2>/dev/null | grep -q .
}

is_dev_stack() {
  docker inspect "${CONTAINER_NAME}" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null \
    | grep -qx '/app/backend'
}

stop_all() {
  "${COMPOSE_DEV[@]}" down 2>/dev/null || true
  "${COMPOSE_DEPLOY[@]}" down 2>/dev/null || true
}

if [[ "${BRING_DOWN}" == "true" ]]; then
  echo "Stopping VAD service..."
  stop_all
  exit 0
fi

if container_running && is_dev_stack; then
  echo "VAD dev stack is running — stopping it before deploy..."
  "${COMPOSE_DEV[@]}" down
elif [[ "${RESTART}" == "true" ]]; then
  stop_all
fi

echo "Deploying VAD from rebuilt image (VAD_RELOAD=false)..."
exec "${COMPOSE_DEPLOY[@]}" up -d --build --force-recreate
