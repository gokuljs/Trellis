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
