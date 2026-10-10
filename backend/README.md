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

By default, local account files are under `~/.trellis/accounts/<user-id>/`.
`TRELLIS_DATA_DIR` changes that root. If a private PR 1 `state.db` exists for
the signed-in account, the backend imports it once into Supabase and leaves
the original file as a backup. The older shared `~/.trellis/state.db` is not
imported. Keep the account directory until the import is verified; it also
contains local provider keys and an import recovery capability.
