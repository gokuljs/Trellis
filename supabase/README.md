# Trellis web database

Apply the numbered SQL files in `migrations/` in order to the Supabase project.
Use the Supabase SQL Editor or the Supabase CLI migration workflow. In the
dashboard, open **Integrations → Data API → Settings**, add `trellis` to
**Exposed schemas**, and click **Save**. Without this setting, PostgREST rejects
Trellis requests with `PGRST106` even after the migrations have been applied.
Never expose `trellis_private`. Do not put a secret or service-role key in Trellis; the
backend uses the project's publishable key together with each user's access
token.

`auth.users` remains Supabase-managed, including its `created_at` and
`updated_at` columns. `trellis.ensure_account()` creates the matching profile,
settings and onboarding rows for a new account. SQLite import support has been
retired. Existing local SQLite files remain untouched and are not imported.

Authenticated users can read only their own rows through RLS. Models are
read-only. No role using a publishable key can write tables directly. The
exposed mutation RPC calls audited functions in the unexposed private schema,
verifies `auth.uid()`, and requires a local run capability for active-run
changes. Only the SHA-256 digest of that capability is stored in
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

Migration `202610130001_retire_legacy_import.sql` removes the import RPCs,
ledger, and temporary account-write gate while preserving Supabase account
data and active chat/run security. Before applying it to an existing project,
finish every import that has started with the previous backend. The migration
checks for unfinished imports and aborts without changing data if any remain.
It does not delete local SQLite source files.

## Local SQL checks

The test fixture `tests/local_bootstrap.sql` supplies only a mock `auth.users`,
`auth.uid()` and the `anon`/`authenticated` roles for a fresh local PostgreSQL
database. **Never apply that fixture to a hosted Supabase project.** CI applies
the five historical migrations and runs the supported runtime SQL contracts,
then tests the import-retirement preflight and migration. The SQL tests use
fixed local-only UUIDs and must run on a disposable database.

Hosted verification still requires applying the migrations to the chosen
project, exposing `trellis`, and testing with two actual Supabase accounts.

Migration `202610150003_model_catalog.sql` stores exact reviewed profiles in the
private schema and adds trusted metadata, verification timestamps, and cached
availability to model rows. Authenticated users can read pending model choices,
but cannot write catalog tables. A trigger prevents enabling arbitrary upstream
IDs or replacing reviewed limits/prices. Missing profiles remain disabled. The
existing model-selection RPCs continue using the enabled-model invariant.

Migration `202610150004_catalog_sync.sql` creates the restricted login
`trellis_catalog_sync` without a password. Provision its credential separately
with the database administrator's interactive `\password trellis_catalog_sync`
command or the operator's secret-management workflow. The migration rejects an
existing privileged role or role memberships. The login has no table reads,
table writes, account RPC access, or trusted-profile mutation grant. It can only
execute `trellis_private.sync_model_catalog(text,jsonb)`, which accepts a bounded
provider availability snapshot and uses reviewed private profiles to determine
readiness. Do not expose the private schema through the Supabase Data API.

The operator connects directly through PostgreSQL with this login. Keep its URL
and provider keys outside the app's public configuration and browser environment.
The worker's deployment supervisor starts and restarts it; the backend app does
not start discovery workers during HTTP requests or application startup.

Apply `202610150007_event_limits.sql` after the chat-model and context-checkpoint
migrations. It backfills private per-run counters and serializes append admission
under the run row lock. Event data enrichment also counts toward the byte cap.
The migration aborts without deleting data if existing history exceeds the caps
or its saved sequence disagrees with its event count; review that history before
retrying. Authenticated callers cannot read or reset the counters, append through
the private helper, or write replay rows directly. Authorized chat deletion still
cascades to its retained run events.
