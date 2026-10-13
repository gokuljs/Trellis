# Trellis backend

FastAPI service for Trellis web chat. Authenticated account data is stored in
Supabase. Provider keys remain in a private account-scoped local `.env` file
and are managed through the Settings API. Local folders must be selected on
each computer before file or command tools can use them.

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

By default, local account files are under `~/.trellis/accounts/<user-id>/`.
`TRELLIS_DATA_DIR` changes that root. Provider keys and local account settings
stay on this computer. Existing SQLite files are left untouched and are not
imported; cloud chats and settings are stored in Supabase.
