# Durable execution contracts

Contract revision C0 establishes the interfaces shared by `durable-acceptance`,
`worker-authority`, `independent-worker`, and `reconnect-experience`. The contract
commit is their shared ancestor. Later changes require coordination among owners.

## Acceptance and public progress

`run.start` keeps its existing parameters. Durable acceptance validates the owned
chat and the submitted request, then atomically creates its input message, run,
ready job, and `run.queued` event. No provider call occurs during acceptance.
The queued run has no execution deadline or execution lease.

Request identity is unique by owner, chat, and `clientRequestId`. The fingerprint
contains trimmed submitted content, `turnId` defaulted to `clientRequestId`, and
explicit `modelId` and `budgetPreset` values with their presence preserved.
Trimming matches Python 3.14 `str.strip()` independently of the PostgreSQL locale.
The shared SQL helper trims only the ends, using U+0009 through U+000D,
U+001C through U+0020, U+0085, U+00A0, U+1680, U+2000 through U+200A,
U+2028, U+2029, U+202F, U+205F, and U+3000. Interior whitespace, U+200B, and
U+FEFF remain unchanged. Fingerprinting and persisted message content use the
same trim rule.
Absent and explicit values differ even when they resolve to the same default.
Store a digest of this canonical input, never the unredacted input. Persist the
redacted content as the message. Look up the request before reading mutable model
settings, provider configuration, or capacity limits. A matching retry returns
the original run. Different input produces `run_conflict`.

`run.lookup` accepts `sessionId` and `clientRequestId`. It returns the existing
`_run_result` shape, or JSON null when the owned chat has no matching run. It never
accepts new work. A foreign chat returns `session_not_found`.

The initial event keeps `run.queued`. Internal claiming does not change that
public status. Atomic `begin` emits `run.started`, records `started_at`, and starts
the existing 600-second or 1,200-second execution budget. `limits.deadlineAt` in
WebSocket responses and `deadline_at` in history are nullable while queued.
Public run states, event names, event versions, and increasing per-run sequences
are preserved. Stored events are authoritative. Persist before notifying local
listeners, and relay cross-process updates through catch-up polling.

One chat permits one active run. Each account permits 16 nonterminal runs.
Enforce both in the acceptance transaction. Existing input limits and budget
presets remain in effect. The durable path initially supports chat only.

## Ownership and worker interfaces

`JobStatus` has four states. A ready job may become claimed, a claimed job may
begin running or return to ready before execution, and a running job finishes
with its public run. Each claim creates an execution attempt. The job has at most
one current attempt. The run and job identify their current worker and attempt.

`ExecutionClaim` contains `job_id`, `run_id`, `worker_id`, `attempt_id`,
`capability`, and database-issued `lease_expires_at`. The worker generates an
attempt UUID and random capability before `claim` and reuses both when reconciling
a lost response. A repeated claim must return the same assignment, not another
job. Persist only capability digests. Bind every execution operation to this
worker, attempt, capability, unexpired lease, run, and allowed state.

`WorkerQueuePort` provides these asynchronous operations.

- `claim(worker_id, attempt_id, capability)` returns a claim or null.
- `begin(claim)` returns the running snapshot. Repeating it does not emit another
  start event or reset the deadline.
- `renew(claim)` returns the snapshot with the new lease expiry.
- `release_unstarted(claim)` returns an unstarted assignment to ready.
- `recover_expired(limit=100)` reconciles at most the requested number of attempts
  and returns the number reconciled.

Reserve worker capacity before claiming. Use short transactions and
`FOR UPDATE SKIP LOCKED`, with consistent run-before-job row lock ordering.
Never hold a database transaction while waiting on a provider or tool.
A begin response must be confirmed before provider interaction, including token
counting and compaction. Losing ownership revokes reads, credential access, and
all writes. Retryable publication carries stable operation identifiers. Completion
saves the final output, events, job, attempt, and run in one transaction.

`WorkerExecutionRepository` adapts existing run, session, and checkpoint methods
to one `ExecutionClaim`. Its implementation must reject unrelated run, session,
model-call, and checkpoint identifiers. Shared inherited methods do not grant
account-wide access or permission to create another run. `get_execution_context`
returns immutable `RunExecutionContext(owner_id, run, session, model)`. Conversation
context is loaded through the assigned session methods. No settings or profile
repository, browser access token, or browser refresh token reaches the executor.

Use the restricted PostgreSQL login `trellis_runtime_worker`. It has explicit
worker-function grants and no direct table access. Worker functions derive the
owner from the assignment and cannot accept a caller-chosen owner. User functions
continue to use authenticated ownership for lookup, cancellation, and approvals.
Old user execution functions must reject worker-managed runs. The scoped credential
adapter checks current ownership and resolves only the assigned provider key.
Jobs and checkpoints never contain provider keys.

An expired claim that never began becomes ready again. An expired attempt that
began becomes interrupted, preserving published partial output. It never executes
a provider step again automatically. Running-step continuation is future work.
Browser disconnect, sign-out, and browser-token expiry do not cancel accepted work.
Only explicit user cancellation or task policy stops it.

## Database names and acceptance interface

PR 1 adds the following columns to `trellis.runs`.

- `execution_mode` is `legacy` or `durable`, with existing rows defaulting to legacy.
- `request_fingerprint` is a nullable digest of the original normalized submission.
- `max_seconds` records the resolved 600-second or 1,200-second budget.
- `worker_id` and `attempt_id` identify the current durable assignment, or null.
- Existing `lease_token` and `lease_expires_at` remain the run ownership fields.
  `lease_token` contains only the capability digest.

`trellis_private.jobs` has `id`, unique `run_id`, `user_id`, `status`, `worker_id`,
`attempt_id`, `lease_expires_at`, `created_at`, and `updated_at`. `status` uses
`ready`, `claimed`, `running`, and `finished`. Ownership and expiry are null for
ready jobs. `run_id` references the owned run.

`trellis_private.execution_attempts` has `id`, `job_id`, `run_id`, `user_id`,
`worker_id`, `status`, `capability_digest`, `lease_expires_at`, `started_at`,
`finished_at`, `outcome`, and `created_at`. Attempt states are `claimed`, `running`,
and `finished`. `outcome` distinguishes released, expired, and public terminal
outcomes. Attempt identifiers are worker-generated UUIDs. Job and run pointers
identify the current attempt. Both private tables deny direct browser access.

`trellis.accept_run(p_payload jsonb)` accepts `session_id`, `client_request_id`,
redacted `content`, resolved `model_id`, resolved `budget_preset`, and `submission`.
The `submission` document contains original trimmed `content`, normalized `turnId`,
and explicit `modelId` and `budgetPreset` only when supplied by the client.
`trellis.match_run_request(p_payload jsonb)` accepts `session_id`,
`client_request_id`, and the same `submission`. It returns the sanitized matching
run, null for a missing request, or a conflict error for a different submission.
The database computes the fingerprint from the normalized JSONB submission.
It never trusts a caller-supplied digest. Original content is transient and is
never stored in a job, event, checkpoint, or run. Only redacted content is persisted.
`trellis.lookup_run(p_session_id uuid, p_client_request_id text)` returns the
owned snapshot or null without checking input or accepting work. All three
functions derive user identity through `auth.uid()` and verify chat ownership.
The Python acceptance service calls `match_run_request` before consulting mutable
configuration. SQL rechecks the submission within `accept_run` to serialize
concurrent duplicate requests. The resolved model and budget must match explicit
submission values when present. Reusing a digest cannot disguise changed input.

## Defaults and delivery ownership

| Setting | Default |
| --- | --- |
| Capacity | 4 runs per worker process |
| Lease | 60 seconds |
| Renewal | Every 15 seconds |
| Queue and cancellation polling | Every 1 second |
| Database error backoff | At most 5 seconds |
| Expiry sweep | At most 100 attempts every 5 seconds |
| Database operation timeout | 5 seconds, with shorter statement and lock timeouts |
| Shutdown drain | 30 seconds while renewing leases |
| Execution budget | Existing 600-second or 1,200-second preset from begin |

`psycopg[binary,pool]==3.3.6` supplies parameterized asynchronous database calls
with bounded connections. Pin its lockfile using uv 0.12.5. The API never silently
falls back to local execution. Stop claiming during shutdown. Release unstarted
assignments and preserve lease renewal during the bounded execution drain.

Migration ownership is reserved in this order.

1. PR 1 owns `202610180001_durable_jobs.sql` for acceptance, jobs, and attempt tables.
2. PR 2 owns `202610180002_worker_authority.sql` for restricted worker authority.
3. PR 3 owns `202610180003_worker_cutover.sql` for activation safeguards if needed.

Durable acceptance stays dormant until the independent-worker integration. Review
and integrate PR 1, PR 2, PR 3, and PR 4 in that order. Development after C0 can
proceed against typed ports and the JSON fixtures in `docs/fixtures/durable`.
Fake-port tests do not replace PostgreSQL authority and complete browser evidence.
