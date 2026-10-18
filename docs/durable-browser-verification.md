# Durable browser verification

The browser suite uses the production frontend, API, user RPCs, worker supervisor,
and restricted PostgreSQL worker login. A test-only Vite alias replaces the
Supabase OAuth SDK, and a test-only API transport calls the same user functions
against a disposable PostgreSQL database. The provider streams deterministic
text without external credentials or model charges. Production bundles do not
include these fixtures.

Use Node 24.13.0, Bun 1.3.10, uv 0.12.5, and an empty PostgreSQL 15 database. Set
`TRELLIS_TEST_DATABASE_URL` to its administrative connection and
`TRELLIS_WORKER_DATABASE_URL` to the same database using `trellis_runtime_worker`.
Never point this suite at a shared application database. Bootstrap refuses a
database that already has the Trellis schema.

```sh
cd backend
uv sync --locked
uv run python -m tests.durable_harness bootstrap
```

Set the restricted role's test password through an administrative connection if
the disposable server requires password authentication. The role has no table
access. Then run:

```sh
cd frontend
bun install --frozen-lockfile
bunx playwright install chromium
bun run test:contracts
bun run test:browser
```

The browser command starts and cleans up its API on port 8123, its Vite server on
3039, and independent worker processes. Keep those ports free. Optional
`TRELLIS_TEST_NODE` selects the Node executable. `TRELLIS_TEST_BACKEND_DIR` allows
development against a predecessor worktree; final verification uses the
integrated backend by default.

The suite records queued acceptance, API restart, two workers, closing and
reopening during partial output, completed-history restoration, deliberately
lost acceptance responses, exact request lookup, sign-out/sign-in, queued
cancellation, interruption with preserved partial output and an explicit retry,
and foreign-user isolation. It asserts no second `run.start` while
reconnecting. PostgreSQL authority and process tests provide additional
permission and worker-death evidence.

Recordings, screenshots, and process logs appear in
`frontend/test-results/durable/`, or `TRELLIS_TEST_ARTIFACT_DIR`. The directory is
ignored by Git and uploaded by CI even when a scenario fails. Authenticated
Supabase OAuth, hosted Render deployment, and provider API availability are
separate operator smoke tests. This suite does not promise running-step recovery.
