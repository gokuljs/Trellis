# Trellis backend

Minimal FastAPI service with a typed health endpoint. The project targets
CPython 3.14 and uses uv for reproducible dependency management.

From this directory:

```bash
uvx --from uv==0.12.5 uv sync --locked
uvx --from uv==0.12.5 uv run fastapi dev
```

The `uvx` form keeps the repository reproducible without changing an older
global uv installation. CI installs the same pinned uv version.

The service is available at `http://127.0.0.1:8000`. Interactive API docs are
served at `/docs`, with the OpenAPI document at `/openapi.json`.
