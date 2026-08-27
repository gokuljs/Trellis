#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backend_pid=""
frontend_pid=""
backend_command=()

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
}

stop_process() {
  local pid="$1"

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  local status=$?

  trap - EXIT INT TERM
  stop_process "$backend_pid"
  stop_process "$frontend_pid"

  wait "$backend_pid" 2>/dev/null || true
  wait "$frontend_pid" 2>/dev/null || true

  exit "$status"
}

trap cleanup EXIT INT TERM

require_command bun

if [[ -x "$repository_root/backend/.venv/bin/fastapi" ]]; then
  backend_command=("$repository_root/backend/.venv/bin/fastapi" dev)
else
  require_command uvx
  backend_command=(uvx --from uv==0.12.5 uv run --locked fastapi dev)
fi

(
  cd -- "$repository_root/backend"
  exec "${backend_command[@]}"
) &
backend_pid=$!

(
  cd -- "$repository_root/frontend"
  exec bun run dev
) &
frontend_pid=$!

echo "Trellis development services started"
echo "Frontend: http://127.0.0.1:3000"
echo "Backend:  http://127.0.0.1:8000"
echo "Press Ctrl-C to stop both services."

while :; do
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    if wait "$backend_pid"; then
      exit 0
    else
      exit $?
    fi
  fi

  if ! kill -0 "$frontend_pid" 2>/dev/null; then
    if wait "$frontend_pid"; then
      exit 0
    else
      exit $?
    fi
  fi

  sleep 1
done
