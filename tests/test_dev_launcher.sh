#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_root="$(mktemp -d)"
dev_pid=""

cleanup() {
  if [[ -n "$dev_pid" ]]; then
    kill -TERM -- "-$dev_pid" 2>/dev/null || true

    for _ in {1..20}; do
      if ! kill -0 -- "-$dev_pid" 2>/dev/null; then
        break
      fi

      sleep 0.1
    done

    if kill -0 -- "-$dev_pid" 2>/dev/null; then
      kill -KILL -- "-$dev_pid" 2>/dev/null || true
    fi

    wait "$dev_pid" 2>/dev/null || true
  fi

  rm -rf -- "$fixture_root"
}

trap cleanup EXIT

mkdir -p \
  "$fixture_root/backend/.venv/bin" \
  "$fixture_root/frontend" \
  "$fixture_root/scripts" \
  "$fixture_root/state" \
  "$fixture_root/test-bin"

cp "$repository_root/Makefile" "$fixture_root/Makefile"
cp "$repository_root/scripts/dev-all.sh" "$fixture_root/scripts/dev-all.sh"

cat >"$fixture_root/backend/.venv/bin/fastapi" <<'EOF'
#!/usr/bin/env bash
stop() {
  echo "stopped" >"$TEST_STATE_DIR/backend.stopped"
  exit 0
}

trap stop INT TERM
echo "$$" >"$TEST_STATE_DIR/backend.pid"
echo "backend ready"
while :; do
  sleep 1
done
EOF

cat >"$fixture_root/test-bin/bun" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" != "run dev -- --ui=stream" ]]; then
  echo "frontend did not start in stream mode: $*" >&2
  exit 64
fi

stop() {
  echo "stopped" >"$TEST_STATE_DIR/frontend.stopped"
  exit 0
}

trap stop INT TERM
echo "$$" >"$TEST_STATE_DIR/frontend.pid"
echo "frontend stream ready"
while :; do
  sleep 1
done
EOF

chmod +x \
  "$fixture_root/backend/.venv/bin/fastapi" \
  "$fixture_root/test-bin/bun"

dry_run_output="$(make --directory "$fixture_root" --dry-run dev)"
if [[ "$dry_run_output" != *"./scripts/dev-all.sh"* ]]; then
  echo "make dev is not wired to the combined launcher" >&2
  exit 1
fi

set -m
TEST_STATE_DIR="$fixture_root/state" \
  PATH="$fixture_root/test-bin:$PATH" \
  make --directory "$fixture_root" dev >"$fixture_root/output.log" 2>&1 &
dev_pid=$!

for _ in {1..50}; do
  if grep -q "frontend stream ready" "$fixture_root/output.log" && \
    grep -q "backend ready" "$fixture_root/output.log"; then
    break
  fi

  if ! kill -0 "$dev_pid" 2>/dev/null; then
    cat "$fixture_root/output.log" >&2
    exit 1
  fi

  sleep 0.1
done

grep -q "frontend stream ready" "$fixture_root/output.log"
grep -q "backend ready" "$fixture_root/output.log"

backend_pid="$(<"$fixture_root/state/backend.pid")"
frontend_pid="$(<"$fixture_root/state/frontend.pid")"

kill -INT -- "-$dev_pid"

for _ in {1..50}; do
  if ! kill -0 "$dev_pid" 2>/dev/null && \
    [[ -f "$fixture_root/state/backend.stopped" ]] && \
    [[ -f "$fixture_root/state/frontend.stopped" ]]; then
    break
  fi

  sleep 0.1
done

if kill -0 "$dev_pid" 2>/dev/null || \
  [[ ! -f "$fixture_root/state/backend.stopped" ]] || \
  [[ ! -f "$fixture_root/state/frontend.stopped" ]]; then
  echo "make dev did not stop both services after SIGINT" >&2
  cat "$fixture_root/output.log" >&2
  exit 1
fi

wait "$dev_pid" 2>/dev/null || true
dev_pid=""

if kill -0 "$backend_pid" 2>/dev/null || kill -0 "$frontend_pid" 2>/dev/null; then
  echo "a development service remained alive after make dev exited" >&2
  exit 1
fi

echo "dev launcher test passed"
