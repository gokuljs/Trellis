# Durable chat execution

After the API accepts a request, its message, run, ready job, and initial event
exist in one database transaction. An independent process claims and executes
that job. Closing the browser, signing out, or restarting the accepting API does
not cancel accepted work. Explicit cancellation remains an authenticated owner
action. Reopening the browser loads saved history and follows stored events.

The public queued state has no execution deadline. The worker confirms atomic
begin before obtaining a provider key or interacting with a provider. Begin
starts the selected 600-second or 1,200-second execution budget. A claim that
expires before begin can be reclaimed. An attempt that expires after begin is
interrupted and preserves published partial output. Running steps do not resume
automatically. This worker supports chat with no local tools.

## Processes and credentials

The API keeps its Supabase URL, publishable key, and verified browser identity.
It does not receive a worker database login. The worker receives
`TRELLIS_WORKER_DATABASE_URL` for `trellis_runtime_worker` and operator-managed
provider secrets. That role has explicit worker function grants, no role
memberships, and no direct access to application tables. Startup rejects a URL
that authenticates as another role, including an administrator. Configure its
password and connection access through your database operator tooling.

Do not store browser tokens or provider keys in jobs or checkpoints. The
execution repository binds reads and writes to the current run, worker, attempt,
capability, lease, and allowed state. Capability values are stored as digests.
The credential adapter verifies a live running assignment and matching provider
before returning its configured key. The API never falls back to local execution
when durable acceptance is unavailable.

## Configuration and startup

Apply migrations through `202610180002_worker_authority.sql` before cutover.
Keep `TRELLIS_ENABLED_CAPABILITIES=[]`. The following controls are explicit.

| Setting | API | Worker |
| --- | --- | --- |
| `TRELLIS_DURABLE_EXECUTION_ENABLED=true` | Use durable execution contracts | Required |
| `TRELLIS_DURABLE_ACCEPTANCE_ENABLED=true` | Permit new submissions | Not used |
| `TRELLIS_WORKER_DATABASE_URL` | Omit | Restricted direct PostgreSQL URL |
| `TRELLIS_WORKER_CAPACITY` | Not used | Default 4 concurrent runs |
| Provider keys | Acceptance validation and model availability | Assigned execution |
| Supabase URL and publishable key | Required | Not used |

Both durable flags default to false for migration compatibility. Set durable
execution true while acceptance remains false to keep history, retry lookup,
and cancellation available while pausing new work. Retries of already accepted
requests still return their run. In durable mode, the older
`POST /api/sessions/{session_id}/turns` endpoint returns
`410 synchronous_execution_retired`. Submit with `run.start` on `/api/runtime`.

From `backend/`, install the locked environment using uv 0.12.5, then run the API
and worker in separate terminals.

```bash
uv run --locked fastapi run
```

```bash
uv run --locked python -m app.worker
```

The worker claims only after reserving a free execution slot. It renews its
60-second lease every 15 seconds, independently of provider output. Queue and
cancellation polling run every second. Failed database operations use bounded
backoff, capped at five seconds. An expiry sweep processes at most 100 attempts
every five seconds. Database operations have a five-second timeout, a shorter
statement timeout, and a shorter row-lock timeout. Transactions end before
provider calls. Connection pools are bounded, with six connections by default.

SIGINT and SIGTERM stop new claims. Unstarted claims are released. Existing work
gets a 30-second drain while renewals continue. Remaining execution is cancelled
and marked interrupted if authority is still valid. A killed process leaves
expiry reconciliation to another worker. Expired or revoked workers cannot
continue publishing.

## Render service recipe and activation

Create a separate Render Background Worker using the same repository revision
and `backend` root directory. Build with the pinned uv installation and
`uv sync --locked`; start with `uv run --locked python -m app.worker`.
Set the restricted database URL, durable execution mode, empty capabilities,
and the configured provider secrets on that service. It has no HTTP port.
Render documents background workers as continuously running services that poll
queues and receive no incoming network traffic.
[Render background workers](https://render.com/docs/background-workers)

Start with one worker process and capacity four. The database claiming mechanism
also supports multiple processes. `SKIP LOCKED` avoids waiting on rows another
consumer is claiming; durable ownership, renewal, and publication fencing supply
the remaining execution guarantees.
[PostgreSQL locking](https://www.postgresql.org/docs/current/sql-select.html)

Activate only after approval of all four dependent changes.

1. Apply the additive schema and worker authorization migrations.
2. Stop new legacy submissions and drain legacy execution.
3. Deploy the updated frontend, API in durable mode with acceptance disabled,
   and the independent worker.
4. Verify restricted role access, configuration, history, and event replay.
5. Enable durable acceptance and prove submit, close, execution, and reopen.

For rollback, pause new durable acceptance and keep the compatible worker drain
path available. Preserve accepted records and do not send them to the old local
executor. Deployment and activation remain operator actions.

Monitor oldest ready-job age, ready-job count, active attempts, lease renewal
failures, interruptions, and saved-event relay failures through trusted database
monitoring and worker logs. Logs omit browser tokens, capabilities, provider
keys, and raw exception messages. A queued request means accepted work is saved;
it does not promise a queue position or estimated start time.

## Repeatable process and browser proof

The test-only `tests.durable_harness` module uses the production API services,
Supabase repository parsing, restricted worker repository, executor, and
supervisor against a disposable migrated PostgreSQL database. It substitutes
only authentication, the PostgREST transport, and the external provider. User
SQL requests still run under `authenticated` with RLS. Worker requests use the
real restricted login. Do not point this harness at a deployed database.

Set `TRELLIS_TEST_DATABASE_URL` to the disposable database's fixture administrator
and `TRELLIS_WORKER_DATABASE_URL` to the same database's restricted login. Bootstrap
an empty database, seed the two test accounts, and start the processes separately.
Skip bootstrap when the disposable database already has the migrations.

```bash
uv run --locked python -m tests.durable_harness bootstrap
uv run --locked python -m tests.durable_harness seed
uv run --locked uvicorn tests.durable_harness:create_api --factory --host 127.0.0.1 --port 8000
```

```bash
uv run --locked python -m tests.durable_harness worker
```

The fixture accepts `alice` and `bob` tokens. `alice:<unix-expiry>` permits an
actual expiry scenario. It seeds completed onboarding and the existing reviewed
OpenAI model. Its deterministic provider waits five seconds before responding
with `Durable worker answer.`. Set `TRELLIS_TEST_PROVIDER_DELAY` to change that
wait. `TRELLIS_TEST_PROVIDER_CHUNK_DELAY` pauses between the two output chunks for
partial-output reconnect tests. Starting the worker after acceptance proves saved queue behavior without
timing assumptions. Production authentication is unchanged.

Run the real subprocess acceptance proof against that disposable database.

```bash
uv run --locked pytest tests/test_worker_processes.py --no-cov -s
```

It restarts the API with queued work, races two actual workers, replays stored
WebSocket events, outlives a browser token, denies another user's controls,
cancels a silent provider, reclaims an unstarted crash, and preserves partial
output without replaying a crashed running step. Backend CI runs this proof
against its PostgreSQL service.

For a browser crash scenario, stop the assigned test worker after partial output,
expire its lease, then start another test worker to reconcile the interruption.
The helper accepts only active durable runs and updates all lease records together.

```bash
uv run --locked python -m tests.durable_harness expire RUN_UUID
```
