# Stage 2: streaming, model discovery, and bounded context

Stage 2 adds incremental answers, durable recovery, reviewed model discovery,
per-chat model selection, and automatic context compaction. It preserves the
existing session and runtime APIs; the web app uses the new bounded history
endpoints while legacy session reads remain available.

## Setup

Use Python 3.14, uv 0.12.5, Node.js 22.12 or newer, and Bun 1.2.4. See the
[backend setup](../backend/README.md), [frontend setup](../frontend/README.md),
and [Supabase setup](../supabase/README.md) for environment variables and startup
commands.

Apply all SQL migrations in filename order to the same Supabase project used by
the frontend and backend. Stage 2 requires the eleven `20261015` migrations below,
after the earlier schema and import-retirement migrations. Expose `trellis` in
the Supabase Data API settings; keep `trellis_private` unexposed. The app uses
the publishable key and the signed-in user's access token. Provider keys belong
only in backend process secrets or the ignored development `backend/.env`.

The default `TRELLIS_ENABLED_CAPABILITIES=[]` runs chat without local tools.
Enable local capabilities explicitly only for a trusted local installation.

| Migration                                                                                                     | Purpose                                                                                                             |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| [`202610150001_assistant_outputs.sql`](../supabase/migrations/202610150001_assistant_outputs.sql)             | Persist streaming and partial assistant output separately from completed transcript messages.                       |
| [`202610150002_output_event_identities.sql`](../supabase/migrations/202610150002_output_event_identities.sql) | Add stable output and event identities for replay and reconciliation.                                               |
| [`202610150003_model_catalog.sql`](../supabase/migrations/202610150003_model_catalog.sql)                     | Store reviewed profiles, verification metadata, and provider availability.                                          |
| [`202610150004_catalog_sync.sql`](../supabase/migrations/202610150004_catalog_sync.sql)                       | Add the restricted discovery login and atomic availability-sync function.                                           |
| [`202610150005_chat_models.sql`](../supabase/migrations/202610150005_chat_models.sql)                         | Persist a model choice per chat and enforce active-run selection rules.                                             |
| [`202610150006_context_checkpoints.sql`](../supabase/migrations/202610150006_context_checkpoints.sql)         | Store immutable, revisioned checkpoints and read checkpoint plus remaining messages in one snapshot.                |
| [`202610150007_event_limits.sql`](../supabase/migrations/202610150007_event_limits.sql)                       | Enforce durable replay count and byte caps with a terminal-event reserve.                                           |
| [`202610150008_history_pages.sql`](../supabase/migrations/202610150008_history_pages.sql)                     | Add stable cursor pages for saved chats and the conversation timeline.                                              |
| [`202610150009_unknown_usage_cost.sql`](../supabase/migrations/202610150009_unknown_usage_cost.sql)           | Clear stale cost estimates when required provider usage is unknown.                                                 |
| [`202610150010_terminal_model_calls.sql`](../supabase/migrations/202610150010_terminal_model_calls.sql)       | Close unfinished calls atomically when their run becomes terminal, including a backfill for existing terminal runs. |
| [`202610150011_reviewed_model_dispatch.sql`](../supabase/migrations/202610150011_reviewed_model_dispatch.sql) | Allow reviewed models to be selected and requested without an availability check.                                   |

Migration 007 aborts without deleting history if existing events exceed its caps
or stored event sequences disagree with their counts. Review that data before
retrying. Migration 010 preserves reported usage, cost, snapshots, and already
finished calls; unfinished calls become failed for a failed run and cancelled
for other terminal run states.

## Model catalog operation

Discovery runs as a separate operator process, never during an HTTP request or
backend startup. Install `psql`, provision a password for the migration-created
`trellis_catalog_sync` login with the administrator's interactive `\password`
command or secret-management workflow, and supply these process secrets:

- `TRELLIS_CATALOG_DATABASE_URL`: direct PostgreSQL URL using only
  `trellis_catalog_sync`; TLS defaults to `verify-full`.
- `TRELLIS_OPENAI_API_KEY` and/or `TRELLIS_ANTHROPIC_API_KEY`: provider list API
  credentials.

From `backend/`, run the worker under the deployment's process supervisor:

```bash
uvx --from uv==0.12.5 uv run --locked python -m app.infrastructure.catalog_sync
```

It refreshes immediately, then waits ten minutes after each completed cycle.
Add `--once` for an externally scheduled job. Keep the database URL out of
command-line arguments. The login can execute the private sync function but
cannot read or write tables, mutate reviewed profiles, or call account RPCs.
Do not give this worker a Supabase service-role key or a user's access token.

Provider list APIs record availability only. A reviewed model can be selected
and requested without running discovery or checking cached availability. Missing
credentials and provider failures are reported when a request is made. Runtime
calls still require reviewed limits, prices, and adapter support for the exact
model ID; unknown IDs never inherit another model's profile.
Reviewed profiles in
[`model_catalog.py`](../backend/app/application/model_catalog.py) and migration
003 must change together when metadata is reviewed.

A successful complete snapshot updates only that provider atomically. A failed,
malformed, oversized, timed-out, or incomplete paginated response preserves its
previous cache. A successful empty snapshot records that provider's availability
without disabling reviewed model choices. Availability timestamps can be
stale during an outage. Each refresh has a 30-second deadline, at most 20 pages
and 2,000 unique IDs, and a 2 MiB response limit. Database writes have a
20-second process deadline, a 15-second statement timeout, and a 3-second lock
timeout; cancellation terminates and joins the child process.

## Incremental answers and recovery

Generation text passes through
[`TextStreamBatcher`](../backend/app/application/streaming.py) before it is saved
or published. Checked text flushes after 100 ms or at 4 KiB of UTF-8 data, with
Unicode character boundaries preserved. Each batch is persisted before live
fanout. Output, model-call, and event identities let replay reconcile the saved
answer without duplicating a completed transcript message.

Streaming checks have a bounded holdback: unfinished words, recognizable
credential forms, and prefixes of configured secrets stay private until their
boundaries are known. Configured literals and recognized credentials are
redacted; an undecided span beyond 8 KiB fails closed. Normal termination
suppresses unfinished credential candidates. A failure can discard unpublished
text, while already published batches remain durable. These checks do not
provide whole-answer validation of content that has already been published.
Portable compaction summaries use a separate fully buffered validation path:
only a complete, bounded, redacted summary may become a checkpoint, and partial
summary text is never shown in the transcript.

Providers normalize termination into final, tool, or incomplete responses. An
incomplete response cannot complete a run. Saved assistant outputs have
`streaming`, `complete`, or `partial` status. Cancellation, failure,
interruption, and storage-limit termination preserve published text as partial
rather than promoting it to a successful answer. The frontend keeps partial
text visible and presents the applicable retry or reconnect state. Stable run
states distinguish waiting/thinking, compacting, answering, tool work, and
finishing.

Each live subscriber queues at most 256 events and 512 KiB of serialized UTF-8
event data. A slow subscriber is detached and receives a replay-required signal
when transport permits. Every socket send, including time spent waiting for the
send lock, has a five-second deadline; a stalled socket is closed so it cannot
block generation. Reconnection resumes from the last durable sequence. The
relay drains persisted events in pages of 500 and checks durable storage for
events missed by in-process fanout.

Replay events remain stored until their chat is deleted. Each run has hard caps
of 8,192 events and 16 MiB of UTF-8 JSON event data; a single payload may be at
most 256 KiB. Ordinary events stop 16 events and 64 KiB before the hard caps so
cancellation and terminal transitions retain finite reserved space. Reaching a
cap stops generation and keeps earlier sequences available for replay. Events
are never evicted to admit new ones. Terminal cleanup can close remaining tool
records without allocating one cleanup event per tool.

## Context budgets and compaction

Before each generation call, the runtime budgets the exact normalized
instructions, messages, tool schemas, and retained tool exchanges. Provider
counting is bounded by a 30-second deadline and the remaining run deadline. If a
portable request cannot be counted, a conservative UTF-8-byte estimate is
explicitly labelled `utf8_bytes_estimate_v1`. Counts are accepted only for the
same input fingerprint. Opaque native state requires a provider count; it cannot
use the byte estimate.

For a reviewed model, usable input is the smaller of the context window minus
the output reservation and any separate provider input cap, minus a safety
margin. The ordinary output reservation is at most 4,096 tokens and never
exceeds the model's output limit or the remaining run budget. The safety margin
is 2% of the full window, with a minimum of 2,048 tokens. Automatic compaction
starts at 80% of usable input, so the threshold is lower than 80% of the full
model window.

Compaction covers a contiguous prefix of complete conversation turns. It never
covers the current request or splits a tool request/result exchange. The tail
retains up to four recent prior turns within a 16,384-token conservative estimate,
plus the current turn. Individual tool results are bounded to 32 KiB for context.
If no safe, affordable prefix exists, the run fails with an explicit context or
budget error instead of silently dropping history.

Every installed checkpoint includes a portable summary, source identities and
hashes, instruction/workspace hashes, model identity, token metadata, and a
revision. Its write atomically validates the owned running lease and completed
compaction call, advances the chat revision, and installs the covered-message
cursors. Idempotent retries keep the same checkpoint; conflicting revisions do
not replace it. Checkpoint creation does not delete transcript messages. The
runtime reads one checkpoint and its unarchived message tail in one snapshot;
it does not reload archived text to build later context.

OpenAI native compaction preserves the complete returned window; Anthropic
native compaction preserves its returned compaction block. A native payload is
used only for the same provider, model, adapter, instructions, and workspace,
with a successful provider count and sufficient budget. A model or provider
switch replays the portable summary and recent tail. Each generation snapshot
records whether replay used `native` or `portable` context.

Native compaction, its portable companion summary, and the following generation
all consume the run's call, token, cost, and deadline budgets. Each native usage
iteration is recorded separately for billing. OpenAI's compact endpoint has no
output cap, so the runtime reserves the reviewed model's maximum output before
dispatch. This can skip native compaction even for a native-capable model;
portable compaction remains available when affordable. Cancellation or a failed
summary does not replace the previous checkpoint.

## Model selection, context gauge, and history

A chat remembers its model independently of the account default. New chats use
the selected draft/default choice; changing an existing chat uses the dedicated
selection mutation. The runtime locks the provider/model identity when a run is
created, and both the API and frontend prevent changing it while that run is
active. Dropdown options show model names only. Availability and credential
flags do not disable them. Saved choices are retained until the user changes
them, and request errors remain visible in the conversation.

The small context gauge beside the composer opens on hover, keyboard focus, or
tap; Escape dismisses its details. It exposes the selected model, active input
count, percentage of the full model window, output reserve, and automatic
compaction threshold. Draft previews are read-only local estimates from
`POST /api/context-preview`, debounced by 300 ms, bounded to 32,000 Unicode
scalar values, and cancelled when superseded. They include instructions, tools,
the portable checkpoint, the bounded remaining transcript, and the unsent draft;
they make no paid provider call. During a run, durable `run.context` events
replace the preview with that call's count and compaction state.

Context occupancy and cumulative billed usage are separate quantities. An
estimate is marked with `~`; missing context is unavailable. Cost uses provider
usage, served tier, cache-read/write details, and applicable long-context rates.
Missing required usage or unpublished tier prices make cost unavailable and
clear any stale estimate. Compaction does not reset cumulative spending or the
saved pricing-context high-water mark where the profile requires it.

React Query owns server reads in a cache scoped to the signed-in session. Reads
receive abort signals, transient reads retry once, mutations do not retry, and
unmount/sign-out cancels and clears the cache. Settings refresh every ten
minutes. The web app uses bounded infinite-query windows instead of eagerly
loading complete saved histories:

- Chat lists and conversation timelines retain at most five pages of 40 rows.
- Saved turn-attempt activity retains at most five pages of 100 rows.
- A saved run's event view retains at most five pages of 100 events.

Conversation and activity queries discard inactive windows with `gcTime: 0`.
Loading older conversation pages is disabled during an active run; “Jump to
latest” resets the history window. Older data remains in the database and can be
requested through cursors. The server accepts history limits from 1 to 100,
defaults to 40 rows, validates cursors up to 512 characters, and preserves stable
ordering across messages and assistant outputs. Legacy endpoints remain
available for compatibility.

## Verification

Run the backend quality checks from `backend/` with the pinned uv version:

```bash
uvx --from uv==0.12.5 uv sync --locked
make check UV='uvx --from uv==0.12.5 uv'
```

This runs Ruff format checking, Ruff linting, and `ty check`. From `frontend/`,
use the existing toolchain:

```bash
bun install --frozen-lockfile
bun run format:check
bun run lint
bun run typecheck
bun run build
```

For hosted rollout, apply the migrations to the chosen project and expose the
`trellis` schema through the Supabase Data API.
