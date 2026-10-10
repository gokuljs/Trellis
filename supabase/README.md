# Trellis web database

Apply the numbered SQL files in `migrations/` in order to the Supabase project.
Use the Supabase SQL Editor or the Supabase CLI migration workflow. In the
project's API settings, add `trellis` to **Exposed schemas**. Never expose
`trellis_private`. Do not put a secret or service-role key in Trellis; the
backend uses the project's publishable key together with each user's access
token.

`auth.users` remains Supabase-managed, including its `created_at` and
`updated_at` columns. `trellis.ensure_account()` creates the matching profile,
settings and onboarding rows for a new account. A second computer may create
these defaults before the old computer imports its account-scoped SQLite file.
Import replaces only untouched generated defaults; cloud edits take
precedence. The older global SQLite database is never imported.

Authenticated users can read only their own rows through RLS. Models are
read-only. No role using a publishable key can write tables directly. The
exposed mutation and import RPCs call audited functions in the unexposed
private schema, verify `auth.uid()`, and require a local run capability for
active-run changes. Only the SHA-256 digest of that capability is stored in
`runs.lease_token`; it is not selectable by the authenticated role. Leases
expire after 60 seconds unless the owning backend renews them. An expired
active run can be marked interrupted and its event appended atomically; it
cannot be resumed on another computer.

Classic chat turns use a separate local capability for their claim, message
writes, and release. Only its SHA-256 digest is stored in `turn_claims`; the
authenticated role cannot select that column. Claims expire after five minutes.

`trellis.list_chat_summaries(p_chat_id)` returns owned chats with visible
message counts in one read request. Omit `p_chat_id` to list all chats; supply
it to load one chat. The function runs with the caller's grants and RLS.

`trellis.import_snapshot(p_snapshot, p_capability)` accepts additive batches of
up to 100 rows and 8 MiB. Its first nonempty profile batch starts the import.
The importer keeps a random capability in a private local file and sends it on
every batch and to `trellis.seal_import(p_capability)`. Supabase stores only its
SHA-256 digest. The private source-row ledger makes exact retries safe; after
sealing, no new rows can be imported. Ordinary account mutations are blocked
between the first batch and seal. Import cannot start while a cloud run or
classic chat turn is active. Accounts without a local PR1 database stay
unsealed so the old computer can import later.

Imported runs, model calls and tool calls must be terminal, with no run lease
or pending approval. Imported run events must be contiguous through the run's
last event and end with the matching terminal event. Import cannot append to a
live cloud run or chat. The source remains untouched as a backup; if it goes
missing during a partial import, restore it before retrying. Provider keys,
test-command presets and the older shared SQLite file are excluded.

## Local SQL checks

The test fixture `tests/local_bootstrap.sql` supplies only a mock `auth.users`,
`auth.uid()` and the `anon`/`authenticated` roles for a fresh local PostgreSQL
database. **Never apply that fixture to a hosted Supabase project.** Apply the
five migrations to that local database, then run `tests/schema_contract.sql`,
`tests/rpc_security.sql`, `tests/runtime_rpc.sql`,
`tests/import_security.sql`, `tests/import_hardening.sql`,
`tests/chat_summaries.sql`, `tests/runtime_regressions.sql` and
`tests/create_run_regressions.sql` with
`psql -v ON_ERROR_STOP=1 -f ...` in that order. The tests use fixed local-only
UUIDs and must run on a disposable database.

Hosted verification still requires applying the migrations to the chosen
project, exposing `trellis`, and testing with two actual Supabase accounts.
