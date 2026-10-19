# Reliable runs in one backend

Send “Explain this code” in Chat A.

1. Trellis checks that you own the chat and looks for the request ID. A request
   ID identifies one submission. Repeating it with matching input returns the
   same run. Changed input returns `run_conflict`.
2. For new work, Trellis checks the model, credentials, budget, and capacity.
   The sign-in token must last for the selected 600 or 1,200 seconds plus a
   30-second margin. A short token is refreshed once before acceptance, using
   the same request ID. This does not sign you out.
3. One database transaction saves the user message, run, request fingerprint,
   and first `run.queued` event. A fingerprint is a hash of the submitted input
   and explicit choices, calculated before secret redaction. It does not store
   another copy of the original message. A transaction saves everything together
   or saves nothing.
4. The backend starts its existing asynchronous execution task. Chat A locks
   Message, Send, model, and budget. Reading, Stop, approvals, and other chats
   remain available. The brief `queued` state means accepted execution is about
   to start; there is no worker queue.
5. Execution saves output batches, model/tool records, events, and the final
   answer in the existing tables. Closing the tab or signing out only disconnects
   progress delivery. The accepted task continues within its limits.
6. Returning to Chat A checks saved server state before enabling submission.
   Trellis restores saved output, replays missed events in order, and follows
   live progress for the same run. It never submits the message again during
   reconnect. A failed status check keeps the chat locked with Reconnect.
7. Completion, cancellation, failure, or interruption unlocks the chat. Stop
   records cancellation intent and cancels owned execution, including a silent
   provider call. Already saved partial answers remain visible.

There is one backend service and one execution process. There are no new `jobs`
or `execution_attempts` tables, separate workers, distributed queue, or execution
checkpoints. The changes add `runs.request_fingerprint` and narrowly extend the
existing database functions. Local account locks remain part of this design.

## Limits and restart behavior

`TRELLIS_MAX_ACTIVE_RUNS` defaults to 16 across the process.
`TRELLIS_MAX_ACTIVE_RUNS_PER_USER` defaults to four. Both accept integers from
1 to 256. One chat still allows only one active run. Approval waits and stopping
runs count until terminal. A matching retry consumes no additional slot.

Excess requests receive `run_capacity` with status 429 before acceptance or a
provider call. They are rejected, not saved for later execution. An uncertain
acceptance response temporarily retains its capacity reservation while the
backend reconciles the original request and execution capability.

The selected deadline includes approval waiting. Approval does not extend it.
Model/tool calls, tokens, spending, output size, and event storage retain their
existing limits. Database access uses verified user tokens held in memory;
browser refresh tokens and broadly privileged database credentials are not stored.

Graceful shutdown stops new acceptance and interrupts unfinished owned work,
including approval waits. After a crash, the previous execution lease expires
within its existing 60-second window. A lease is temporary permission to publish
execution results. Reading/reconnecting to an expired run marks it interrupted
and preserves its saved partial output. No interrupted run is restarted
silently. Retry is an explicit new submission. Checkpoints and safe continuation
belong to the next stage.

The legacy `POST /api/sessions/{session_id}/turns` endpoint returns 410 for an
owned chat. Use WebSocket `run.start`. `run.resume` follows saved work and never
restarts computation; `run.cancel` requests stopping. `run.lookup` takes
`sessionId` and `clientRequestId` and returns the owned run or `null`.

## Verification

From `backend/`, run `make check` with uv 0.12.5. Keep the repository's current
strict pytest and coverage configuration. From `frontend/`, run formatting,
linting, type checking, build, and `bun test apps/web/tests`. Builds need the
usual explicit API and WebSocket environment variables.

For PostgreSQL contracts, create a new empty disposable database and point the
standard `PGHOST`, `PGPORT`, `PGUSER`, and `PGDATABASE` variables at it. Run these
commands from the repository root:

```bash
psql -X -v ON_ERROR_STOP=1 -f supabase/tests/bootstrap.sql
for migration in supabase/migrations/*.sql; do
  psql -X -v ON_ERROR_STOP=1 -f "$migration"
done
psql -X -v ON_ERROR_STOP=1 -f supabase/tests/reliable_runs.sql
psql -X -v ON_ERROR_STOP=1 -f supabase/tests/run_stopping.sql
bash supabase/tests/reliable_runs_concurrency.sh
```

The bootstrap imitates Supabase's auth objects for local tests. Never apply it
to a hosted project. The contract scripts verify duplicate acceptance, rollback,
ownership, capability fencing, partial output, approval deadlines, and concurrent
requests. Backend CI runs these scripts against PostgreSQL 15.

The browser fixture uses deterministic service substitutes and the actual UI:

```bash
cd frontend
bun x vite --config apps/web/tests/browser/chat-routing.config.ts
```

Open `http://127.0.0.1:3002` and select **Run routing checks**. The checks cover
chat isolation, slow/failed discovery, saved progress, locking, Stop, reconnect,
response loss, temporary failures, and completion during discovery. The fixture
makes no provider calls or external account changes.

Before deployment, apply only the two new production migrations through the
normal operator process, and validate the configured capacity on the target
host. Keep one API process and one service instance. The demo Render free plan
can sleep or restart; saved history survives, running execution may be interrupted.
Merging, hosted migrations, and deployment remain operator actions.
