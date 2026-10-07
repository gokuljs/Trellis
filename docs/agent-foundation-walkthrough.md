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
