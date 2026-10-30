# Trellis backend instructions

The backend is a minimal FastAPI application. Keep this phase production-shaped
but intentionally small; do not introduce runtime orchestration, authentication,
databases, queues, containers, CORS, or model-provider SDKs without an explicit
request. Use the repository's single pstack skill for engineering workflows.

## Boundaries

- `app/api` owns HTTP routes, request/response models, and transport wiring.
- Future application services own orchestration and lifecycle use cases.
- Future domain modules own invariants and typed events without FastAPI imports.
- Provider adapters must implement narrow protocols and keep third-party SDK
  types at the integration boundary.

## Safe implementation workflow

- Use pstack's bug-fix and `tdd` workflows when a focused test is practical.
  Skip an unclear or expensive failing-test path and verify with the closest
  useful check.
- Use synchronous path operations for blocking work; use `async` only when the
  entire call path is non-blocking.
- Bound concurrency and retries, set explicit timeouts, and propagate
  cancellation through long-running operations.
- Keep runtime dependencies minimal and compatible with CPython 3.14. Update
  `uv.lock` with the pinned uv version whenever dependencies change.

## Review priorities

Review public API and OpenAPI compatibility first, then type safety, cancellation
and timeout behavior, dependency risk, test coverage, and accidental layering
leaks. Do not invent contracts for future Trellis runs, sessions, or events.

## Required checks

From this directory, run:

```bash
make check
```

The command must pass Ruff format checking, Ruff linting, and `ty check`.
Use uv 0.12.5.
