---
name: develop-trellis-runtime
description: Design or implement Trellis orchestration, run lifecycles, typed events, tool execution, and provider adapters. Use only when a task changes the Trellis agent runtime or its execution contracts; do not use for generic FastAPI setup, CRUD endpoints, or unrelated frontend work.
---

# Develop the Trellis runtime

Use this skill for changes to the execution engine that will eventually coordinate
agent runs, sessions, typed events, tools, or model/provider adapters. Keep this
initial backend intentionally small: do not invent runtime APIs, persistence
schemas, event names, or provider behavior that the repository has not defined.

## Establish the contract first

1. Read the repository-root and nearest `AGENTS.md` files before editing.
2. Inspect existing modules, tests, and public API documentation. Treat current
   behavior as the contract; record unknowns instead of guessing.
3. Define the smallest typed boundary required by the task. Preserve backward
   compatibility unless the request explicitly changes a public contract.

## Keep the architecture separated

- Keep HTTP/FastAPI transport concerns in the API layer.
- Keep orchestration and lifecycle state in application services.
- Keep domain state, invariants, and typed events independent of transport.
- Put model/provider integrations behind narrow protocols and adapters; do not
  leak SDK types into domain code.
- Pass dependencies explicitly so tests can use deterministic fakes.

## Make async work bounded and cancellable

- Use async only for genuinely non-blocking work; move blocking SDK/file work
  behind an appropriate boundary.
- Bound concurrency, queues, retries, and provider timeouts. Never create an
  unbounded task or retry loop.
- Propagate cancellation and clean up resources with structured lifetimes.
- Preserve event ordering and idempotency where the existing contract requires
  it; document assumptions when it does not.

## Work test-first

Follow RED–GREEN–REFACTOR for each behavior. Start with a focused failing test,
run it to confirm the expected failure, implement the smallest change, then
refactor only while the full suite remains green. Test cancellation, timeout,
provider failure, and duplicate-event behavior when those paths are in scope.

## Verify with evidence

Run the narrowest relevant tests after each change, then the backend aggregate
check before completion. Include formatting, linting, type checking, coverage,
and contract/OpenAPI checks as applicable. Diagnose failures from their root
cause before changing code, and report commands and results rather than relying
on assumptions.
