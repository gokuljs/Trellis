# Building the Trellis agent foundation

This is a reading guide to the `agent-foundation` branch. Its numbered sections
follow the commits in order. Each section describes the code that exists at that
point in history, so checking out a commit and reading the guide at that commit
shows the same stage of the build.

## Before step 1: the text-only run

A chat message enters through the browser's runtime WebSocket. `RunService`
creates a durable run and records one user message. It sends the saved transcript
to one provider, streams text and usage events, stores one model-call record,
then saves one final assistant message. The browser can replay run events while
its page remains open. The run does not yet use tools or take another model step.

Read this baseline in order: `frontend/apps/web/src/lib/runtime-client.ts`,
`backend/app/api/routes/runtime.py`, `backend/app/application/runs.py`,
`backend/app/infrastructure/providers.py`, and
`backend/app/infrastructure/database.py`.

## Step 1 — Add agent tool-call domain contracts

**Why this step exists.** The provider adapters and run service need one shared
language for tools before either provider's wire format is translated. This
contract belongs in Trellis' domain rather than in an OpenAI or Anthropic type.

**What works now.** Chat still follows the text-only flow above. Internally, a
model request can now carry stable instructions and available tool definitions.
The conversation type can represent an assistant tool request and its matching
tool result. A streamed model event can carry a completed tool call. Nothing
executes a tool yet.

**Follow the code.** `ModelRequest` carries the instructions, history, and tool
specifications. `ModelMessage` represents each user, assistant, or tool item.
`ModelToolCall` contains the provider-neutral call ID, name, and decoded
arguments; `ModelStreamEvent(kind="tool_call")` hands it to the runtime only
when the call is complete. These types live together in
`backend/app/domain/runtime.py`. The existing user-visible `Message` type stays
as user/assistant, so this change does not alter the session transcript API.

**Verification.** The new domain tests first failed because `ModelToolCall` did
not exist. After implementation, `tests/test_runtime_domain.py` passed (10
tests). The pinned uv 0.12.5 `make check` passed Ruff formatting, Ruff linting,
`ty`, and 99 backend tests with 91.31% coverage; pytest reported three existing
dependency/resource warnings.

**Next.** Translate each provider's streamed tool-call format into this shared
contract, one provider at a time.

## Step 2 — Parse OpenAI streamed function calls

**Why this step exists.** OpenAI sends function calls as several stream events:
an item announcement, pieces of an argument string, and a final item. The
runtime must receive one validated Trellis call only after the whole response
completes. Otherwise a partial or malformed request could reach a local tool.

**What works now.** The OpenAI adapter can send tool definitions and stable
instructions, read multiple streamed function calls, and replay assistant calls
with their tool results on the next request. It also carries encrypted reasoning
items through an opaque domain type for stateless GPT-5.5 follow-ups. The run
service still makes a single text-only model call, so no tool executes yet.

**Follow the code.** `ModelContinuationItem` in
`backend/app/domain/runtime.py` carries provider-owned reasoning state without
teaching the domain OpenAI's format. In
`backend/app/infrastructure/providers.py`, `OpenAIProvider.stream` converts
`ModelRequest` into the Responses input, then `_stream_openai` tracks item and
call IDs, assembles arguments, checks their size and nesting, parses a JSON
object, and emits `ModelStreamEvent(kind="tool_call")` after
`response.completed`. It rejects duplicate, truncated, and inconsistent items
with a safe provider error. Completed reasoning items are replayed ahead of the
assistant call and result on the next stateless request.

**Verification.** Focused provider tests first failed for the missing call
parsing, normal `arguments.done` events without a name, malformed arguments,
and reasoning replay. The first pinned aggregate check exposed a stack-size
dependent deep-JSON test; the parser now rejects nesting beyond 64 levels
directly. The final uv 0.12.5 `make check` passed Ruff formatting, Ruff linting,
`ty`, and 118 backend tests with 90.66% coverage. Pytest reported one existing
Starlette/httpx deprecation warning.

**Next.** Translate Anthropic's streamed `tool_use` blocks into the same Trellis
call type.

## Step 3 — Parse Anthropic streamed tool use

**Why this step exists.** Anthropic sends a tool request inside an assistant
content block, with its input arriving as partial JSON. Its next request also
needs the assistant's `tool_use` blocks followed immediately by a user message
containing the matching `tool_result` blocks. The runtime needs the same
provider-neutral calls it receives from OpenAI.

**What works now.** The Anthropic adapter accepts tool definitions, stable
instructions, mixed text and tool blocks, and multiple tool requests. It can
replay an assistant message and its grouped tool results. Chat still uses one
model call and does not yet execute tools; these are adapter capabilities ready
for the later run loop.

**Follow the code.** In `backend/app/infrastructure/providers.py`,
`_anthropic_messages` converts ordered Trellis messages into Anthropic's
assistant and user content blocks. `_stream_anthropic` tracks each block by
index. `_AnthropicToolUse` collects `input_json_delta` fragments and validates
one complete JSON object when the block stops. Calls are emitted as
`ModelStreamEvent(kind="tool_call")` only after `message_stop` confirms a
complete tool-use response. Duplicate IDs, excessive or malformed arguments,
unclosed blocks, and a `max_tokens` stop during tool use return a safe provider
error.

**Verification.** The first focused test failed because the request had no
Anthropic `system` field. After implementation, 14 Anthropic stream tests
passed, including mixed blocks, grouped results, tool-only completion, and
invalid streams. The final uv 0.12.5 `make check` passed Ruff formatting, Ruff
linting, `ty`, and 131 backend tests with 90.22% coverage. Pytest reported the
existing Starlette/httpx deprecation warning and a SQLite resource warning.

**Next.** Save ordered assistant and tool exchanges so a later model call can
rebuild its context after each step.

## Step 4 — Persist ordered agent exchanges

**Why this step exists.** A multi-step run needs to remember every assistant
tool request and every tool result in the order they happened. The existing
session transcript has room for the user's message and the final assistant
answer, so intermediate work needs its own durable record.

**What works now.** SQLite stores ordered assistant and tool messages for one
run, each tool call's arguments and status, and an approval decision if one has
been made. It also stores the normalized model request and response, including
usage and opaque provider continuation items. The visible chat transcript still
contains only the user message and final answer. Ordinary chat works as before;
no local tool executes yet. If a provider asks the current text-only run for an
unoffered tool, the run records that response and fails without showing its
intermediate text as a final reply.

**Follow the code.** SQLite migration 6 in
`backend/app/infrastructure/database.py` adds `run_messages` and `tool_calls`.
`record_assistant_message` saves the assistant item, its calls, and replayable
events in one transaction. `record_tool_result` and
`record_tool_approval_decision` update each call and event log atomically.
`list_run_messages` rebuilds the ordered exchange, including opaque reasoning
items. The repository methods are declared in
`backend/app/application/ports.py`; their records and statuses are in
`backend/app/domain/runtime.py`. `RunService` in
`backend/app/application/runs.py` now saves full normalized model snapshots.

**Verification.** The storage tests first failed for missing ordered records;
new round-trip and model-record assertions then failed for lost continuation
items and incomplete snapshots. A focused test also caught the text-only run
incorrectly treating an unsolicited tool request as a final reply. After those
changes, the final uv 0.12.5 `make check` passed Ruff formatting, Ruff linting,
`ty`, and 136 backend tests with 90.08% coverage. Pytest reported the existing
Starlette/httpx deprecation warning and a SQLite resource warning.

**Next.** Attach an optional folder to a session so local tools have a clear
workspace boundary.

## Step 5 — Attach workspaces to sessions

**Why this step exists.** File and Git tools need one explicit folder boundary.
A session owns that choice so the user can see and change it, while ordinary
chat can continue without a folder.

**What works now.** The chat UI accepts an absolute folder path, shows its
canonical location, and lets the user change or remove it. Trellis checks that
the folder exists and is a directory. A queued or active run prevents a folder
change so one run cannot switch workspaces mid-flight. Sessions without a
workspace still work normally. The current text-only run does not use the
folder yet.

**Follow the code.** `WorkspaceAttachment` in
`frontend/apps/web/src/components/workspace-attachment.tsx` collects the path;
`app-shell.tsx` saves it before allowing chat submission. `frontend/apps/web/src/lib/api.ts`
sends it through the session API. `backend/app/api/routes/sessions.py` accepts
the optional value, and `SessionService` in
`backend/app/application/sessions.py` resolves it off the async event loop.
SQLite migration 7 in `backend/app/infrastructure/database.py` saves the path
and atomically rejects changes while a run can still use that session.

**Verification.** Focused backend tests first failed for missing workspace
storage and validation; focused frontend tests first failed for the missing
control and for a submission racing with a folder save. After integration, the
uv 0.12.5 backend `make check` passed Ruff formatting, Ruff linting, `ty`, and
141 tests with 90.14% coverage. Frontend Prettier check, lint, typecheck, 35
tests, and production build passed. The machine's default Node 20.11.1 could
not start this Vite/Vitest version; the tests and build passed with its installed
Node 24.13.0. Pytest reported the existing Starlette/httpx warning and SQLite
resource warnings.

**Next.** Add bounded workspace list, search, read, and Git inspection tools
that can use this saved root.

## Step 6 — Add workspace read and Git tools

**Why this step exists.** Once a session has a folder, the agent needs a small
set of safe ways to inspect it. These tools establish the workspace boundary,
argument checks, output limits, and error shape before the run loop uses them.

**What works now.** The backend can list workspace files, search literal text,
read numbered lines, and inspect Git status, unstaged diff, or recent commits.
Each call returns a bounded `ToolResult` with a safe error code when it fails.
Generated folders and common credential files are skipped. Chat still makes
one model call; these tools are not offered to that call yet.

**Follow the code.** `ToolResult` in `backend/app/domain/runtime.py` is the
provider-neutral result. `ToolRegistry` in `backend/app/application/tools.py`
publishes four strict JSON schemas, validates a completed `ModelToolCall`,
redacts common secret patterns, and caps its result. `LocalReadToolExecutor`
in `backend/app/infrastructure/local_tools.py` resolves the saved root and
dispatches a tool. File traversal uses directory descriptors and rejects
symlinks and protected paths; search and read cap scanned bytes, lines, time,
and output. Git runs only fixed inspection commands in a separate process
group, with a minimal environment, timeout, and output cap. A caller receives
the shared result and can later save it before asking the model to continue.

**Verification.** Focused tests covered each tool, schema rejection, path
confinement, secret filtering, Git failures, timeouts, and output bounds. The
first integrated aggregate run passed the tests but found only 89.42%
coverage; additional boundary tests brought it above the required floor. The
final uv 0.12.5 `make check` passed Ruff formatting, Ruff linting, `ty`, and
163 backend tests with 90.24% coverage. Pytest reported the existing
Starlette/httpx deprecation and SQLite resource warnings.

**Next.** Build one deterministic model context from the visible transcript,
saved agent exchange, workspace guidance, and these tool definitions.

## Step 7 — Build deterministic model context

**Why this step exists.** Each model step needs the same ordered view of the
conversation, even after a run pauses or resumes. The runtime also needs one
stable instruction version and a clear place for workspace guidance and tool
definitions.

**What works now.** A context builder assembles the visible session messages,
then the current run's saved assistant and tool messages, in ordinal order. It
preserves tool-call IDs and provider continuation items. With an attached
workspace it includes sorted tool definitions, the root path, and bounded
root `AGENTS.md` guidance if supplied; without one it offers no local tools.
The guidance is treated as project data and common secret patterns are
redacted. The live run still uses its existing text-only request; Step 8 will
call this builder.

**Follow the code.** `build_model_context` in
`backend/app/application/context.py` converts `Message` and
`RunMessageRecord` into provider-neutral `ModelMessage` items, sorts each
source, and returns versioned instructions plus tool schemas. It translates
each saved internal tool-call ID back to the provider call ID before replay;
an unmatched result is rejected. It rejects
guidance without a workspace or above 16 KiB. `read_workspace_guidance` in
`backend/app/infrastructure/local_tools.py` reads only a regular root
`AGENTS.md`, without following a symlink or accepting oversized or non-text
content. The builder uses `redact_secrets` from
`backend/app/application/tools.py` before guidance can enter a model request.

**Verification.** Focused tests first failed for missing guidance support and
then for an unredacted key and missing guidance reader. Review found that a
saved tool result held Trellis' internal call ID rather than the provider's;
a database-to-context replay test failed until the builder translated it.
The final uv 0.12.5 `make check` passed Ruff formatting, Ruff linting, `ty`,
and 169 backend tests with 90.40% coverage. Pytest reported the existing
Starlette/httpx deprecation and SQLite resource warnings.

**Next.** Use this context for a run that can make several model calls, execute
tools between them, and save each step before moving on.

## Step 8 — Execute multi-step agent runs

**Why this step exists.** The adapters can decode tool requests, the database
can save an ordered exchange, and the registry can safely read a workspace.
This step connects those pieces so one user turn can inspect files and then ask
the model to answer using the result.

**What works now.** With a workspace attached, a run can make several model
calls and execute `list_files`, `search_files`, `read_file`, and `inspect_git`
between them. It can handle several tool calls in one response, including tool
errors. Each model call, intermediate assistant message, tool request, tool
result, and event is saved before the next model call. Only the final answer
appears in the visible session transcript. Without a workspace, ordinary chat
still works and no local tools are offered. Tool requests from a model without
an attached workspace fail the run safely.

**Follow the code.** `RunService._execute` in
`backend/app/application/runs.py` loads the session workspace and root guidance,
builds context from the visible transcript and saved run exchange, then records
a model call before streaming its response. A completed tool request is checked
against the offered tool names and call limits, saved with its assistant
message, executed through `ToolRegistry`, and saved as a result before the loop
builds the next request. `backend/app/main.py` wires the registry and guidance
reader. SQLite migration 8 in `backend/app/infrastructure/database.py` enables
tool support for the built-in models; restart recovery closes unfinished tool
calls. `RunService` closes pending calls on failure or cancellation and keeps
each tool within the run deadline.

**Events and records.** A `model.completed` event marks every completed model
step, and `tool.call` and `tool.result` events show the intermediate work. The
WebSocket method and event shapes stay the same. Final `assistant.delta` text
is emitted after a model step completes: the runtime must first know whether
text belongs to a final answer or to a tool-requesting assistant message, and
redacting a complete response prevents secrets split across stream fragments
from leaking into events. Tool arguments and model snapshots are redacted;
secret-looking call IDs are rejected. Tool and continuation streams have size
limits, and a truncated tool result tells the next model step that it is
incomplete. The existing run row currently supplies provisional 8-model-call,
16-tool-call, and three-minute bounds; the next commit replaces these with
selectable budget presets and cost accounting.

**Verification.** The first integration test failed because the run service
still made one text-only call. Focused tests then caught split-token leakage,
an incomplete tool result on cancellation or restart, unbounded tool execution,
raw provider metadata, secret-looking call IDs, excess tool and continuation
events, and a cancellation race. Tests cover two model steps, multiple tools,
tool-only responses, safe errors, a failed second model call, and a truncated
result reaching the next step. The final uv 0.12.5 `make check` passed Ruff
formatting, Ruff linting, `ty`, and 183 backend tests with 90.38% coverage.
Pytest reported the existing Starlette/httpx deprecation and SQLite resource
warnings.

**Next.** Give each run a named budget with clear limits and estimated cost.
