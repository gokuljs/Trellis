# Trellis backend

FastAPI service for Trellis web chat. Authenticated account data is stored in
Supabase. Provider keys come from backend secrets managed by the operator.
Users sign in, choose an available model, and chat.

From this directory:

```bash
uvx --from uv==0.12.5 uv sync --locked
uvx --from uv==0.12.5 uv run fastapi dev
```

The `uvx` form keeps the repository reproducible without changing an older
global uv installation. CI installs the same pinned uv version.

Copy `.env.example` to `.env` and set `TRELLIS_SUPABASE_URL` and
`TRELLIS_SUPABASE_PUBLISHABLE_KEY` for the same project used by the web app.
Apply the SQL migrations in `../supabase/migrations/` and expose `trellis` in
Supabase's API settings. The service is available at `http://127.0.0.1:8000`.
Interactive API docs are served at `/docs`, with the OpenAPI document at
`/openapi.json`.

## Runtime capabilities

The backend starts in chat-only mode with `TRELLIS_ENABLED_CAPABILITIES=[]`.
Folder selection and local tools are unavailable in this mode, including for
chats that already have saved folder paths or tool approvals.

For a trusted local installation, explicitly enable the capabilities you need,
for example `TRELLIS_ENABLED_CAPABILITIES=["local_read","local_patch","local_command"]`.
Restart the backend after changing this setting. Account preferences and tool
approvals cannot enable a capability. Keep the empty list for hosted chat.

## Provider credentials

Set `TRELLIS_OPENAI_API_KEY` and/or `TRELLIS_ANTHROPIC_API_KEY` in the backend's
deployment secrets. For development, these may be set in the ignored
`backend/.env` file. Never put provider keys in frontend environment variables,
source control, or the browser bundle. Models with a configured provider are
available to signed-in users; configuration status does not verify a key with
the provider. The settings API returns status only, with no key hints.

To replace or rotate a key, update the server secret and restart every backend
process. Revoke the old key with the provider after replacement, or immediately
if it is compromised. To remove access, delete the server secret and restart
every backend process; revoke the key upstream to stop any request still using
it. Account preferences cannot replace or delete server credentials. The old
key-changing API routes return `403 provider_credentials_managed`; onboarding
accepts only a model selection. Encrypted user-owned credentials are deferred.

Old account `.env` and SQLite files are left untouched and are no longer read
for credentials or imported. Remove old key files yourself when no longer
needed. `TRELLIS_DATA_DIR` controls account lock files under
`~/.trellis/accounts/<user-id>/`; chats and settings are stored in Supabase.

## Session and service errors

The runtime socket authenticates before accepting run requests, checks access
again before each request, and checks idle connections every 30 seconds.
Invalid credentials return an error with status `401` and close code `4401`.
Temporary authentication or data failures return status `503` and close code
`1013`, so the web app can offer a retry without clearing a valid session.
Recheck errors use a JSON-RPC error with `id: null` before closing the socket.
Authentication and account checks have a five-second deadline.

Model choices include verified context/output limits, native-compaction support,
metadata readiness, verification sources and dates, and the latest cached
provider availability. Settings includes pending choices with `selectable=false`
and a reason; runtime model lookup returns only enabled, verified choices.
Unknown IDs never inherit another model's limits or prices. The existing two
choices remain enabled until a successful discovery refresh confirms availability.

`app/application/model_catalog.py` owns reviewed first-party token prices and
limits as of 2026-10-15. The matching database documents live in migration
`202610150003_model_catalog.sql`; changes to a profile must update both together.
Input budgets reserve up to 4096 output tokens, keep a 2% safety margin (at least
2048 tokens), and compact at 80% of usable input space. A provider's separate
input cap can reduce that space. Pricing distinguishes cache reads, five-minute
and one-hour writes, long-context thresholds, and the actual served tier.
Unpublished tier prices or missing required usage produce an unavailable cost.
Legacy calls without an Anthropic write TTL retain the conservative one-hour
price. GPT-5.5 callers retain the highest session pricing context after compaction.
Token estimates exclude provider tools, regional premiums, taxes and negotiated
rates.
