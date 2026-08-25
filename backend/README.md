# Trellis backend

FastAPI service for Trellis' local-first chat. It stores the installation
profile, sessions, and complete transcripts in SQLite. Provider keys are kept
separately in the private app-data `.env` file and are managed through the
Settings API.

From this directory:

```bash
uvx --from uv==0.12.5 uv sync --locked
uvx --from uv==0.12.5 uv run fastapi dev
```

The `uvx` form keeps the repository reproducible without changing an older
global uv installation. CI installs the same pinned uv version.

The service is available at `http://127.0.0.1:8000`. Interactive API docs are
served at `/docs`, with the OpenAPI document at `/openapi.json`.

By default, local state is written to `~/.trellis/state.db` and secrets to
`~/.trellis/.env`. Set `TRELLIS_DATA_DIR` to use another directory. Deleting
that directory resets the installation ID and all local data.
