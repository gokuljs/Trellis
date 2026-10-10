# Trellis web database

Apply the numbered SQL files in `migrations/` in order to the Supabase project.
Use the Supabase SQL Editor or the Supabase CLI migration workflow. In the
project's API settings, add `trellis` to **Exposed schemas**. Never expose
`trellis_private`. Do not put a secret or service-role key in Trellis; the
backend uses the project's publishable key together with each user's access
token.

`auth.users` remains Supabase-managed, including its `created_at` and
`updated_at` columns. `trellis.ensure_account()` creates the matching profile,
settings and onboarding rows for a new account. The account-scoped SQLite
import runs before `ensure_account()` for existing users, so default rows do
not conflict with historical account data. The older global SQLite database
is never imported.

Authenticated users can read only their own rows through RLS. Models are
read-only. No role using a publishable key can write tables directly. The
exposed mutation and import RPCs call audited functions in the unexposed
private schema, verify `auth.uid()`, and require a local run capability for
active-run changes. Only the SHA-256 digest of that capability is stored in
`runs.lease_token`; it is not selectable by the authenticated role. Leases
expire after 60 seconds unless the owning backend renews them. An expired
active run can be marked interrupted and its event appended atomically; it
cannot be resumed on another computer.

`trellis.import_snapshot()` accepts additive batches of up to 100 rows and
8 MiB. It records source row hashes in a private ledger so retries do not
overwrite cloud changes. Imported runs, model calls and tool calls must be
terminal, with no run lease or pending approval. This import RPC is intended
only for the PR1 per-user SQLite snapshot and does not import provider keys,
test-command presets, or the older shared SQLite file.

## Local SQL checks

The test fixture `tests/local_bootstrap.sql` supplies only a mock `auth.users`,
`auth.uid()` and the `anon`/`authenticated` roles for a fresh local PostgreSQL
database. **Never apply that fixture to a hosted Supabase project.** Apply the
three migrations to that local database, then run `tests/schema_contract.sql`,
`tests/rpc_security.sql`, `tests/runtime_rpc.sql` and
`tests/import_security.sql` with `psql -v ON_ERROR_STOP=1 -f ...` in that
order. The tests use fixed local-only UUIDs and must run on a disposable
database.

Hosted verification still requires applying the migrations to the chosen
project, exposing `trellis`, and testing with two actual Supabase accounts.
