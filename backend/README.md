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
