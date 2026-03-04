#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: dev.sh <command>

Commands:
  mypy      Run mypy type-checker
  test      Run pytest test suite
  cover     Run pytest with coverage report
  build     Rebuild the agentception Docker image
  restart   Restart the agentception container
  logs      Tail agentception container logs
  migrate   Run Alembic database migrations
  shell     Open a bash shell inside the container
  help      Show this help message
EOF
}

CMD="${1:-help}"

case "$CMD" in
  mypy)
    docker compose exec agentception mypy .
    ;;
  test)
    docker compose exec agentception pytest
    ;;
  cover)
    docker compose exec agentception pytest --cov=. --cov-report=term-missing
    ;;
  build)
    docker compose build agentception
    ;;
  restart)
    docker compose restart agentception
    ;;
  logs)
    docker compose logs -f agentception
    ;;
  migrate)
    docker compose exec agentception alembic upgrade head
    ;;
  shell)
    docker compose exec agentception bash
    ;;
  help)
    usage
    ;;
  *)
    echo "Unknown command: $CMD. Run 'dev.sh help' for usage." >&2
    exit 1
    ;;
esac
