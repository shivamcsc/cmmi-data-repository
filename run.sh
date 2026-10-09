#!/usr/bin/env bash
# Start the CMMI data repository.
set -e
cd "$(dirname "$0")"
exec ./.venv/bin/uvicorn app.main:app --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8000}" "$@"
