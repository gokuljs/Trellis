# Trellis

Trellis is a self-improving multi-agent system for coding and research. It coordinates specialized agents that can work together, learn from previous tasks, and continuously improve how they solve problems.

## Local chat quick start

Apply the SQL files in `supabase/migrations/` to your Supabase project in order
and expose the `trellis` schema. Copy `backend/.env.example` to `backend/.env`
and `frontend/.env.example` to `frontend/.env`. Set the same Supabase project
URL and publishable key in both files. Then install dependencies and start both
services from the repository root:

```bash
uvx --from uv==0.12.5 uv sync --locked --directory backend
bun install --cwd frontend --frozen-lockfile
make setup-hooks
make dev
```

The frontend is served at `http://127.0.0.1:3000` and the backend at
`http://127.0.0.1:8000`. The command uses plain streamed logs so both services
remain readable in one terminal. Press `Ctrl-C` once to stop both services.

Then open `http://127.0.0.1:3000`, sign in, visit Settings, and add an OpenAI
or Anthropic key. Chats, settings, and run history are stored in Supabase for
the signed-in account. Provider keys and local workspace selection stay on the
computer. `TRELLIS_DATA_DIR` controls where the backend keeps local keys and
any private account-scoped SQLite file awaiting one-time import. The older
shared SQLite file is left untouched; see [the migration guide](supabase/README.md).

## Formatting and pre-commit checks

`make setup-hooks` installs the pre-commit hook for the current checkout or
worktree. Run it once in each new worktree after installing the frontend and
backend dependencies. The hook formats staged frontend files with Prettier and
staged backend Python files with Ruff, then keeps the formatted result in the
commit. If formatting a partially staged file cannot be safely merged back into
the working copy, the hook warns and preserves the unstaged edits; run
`make format` afterward to synchronize the working copy.

Run `make format` to format all supported frontend files and backend Python, or
`make format-check` to verify formatting without changing files. The hook checks
formatting only; lint, type, test, and build checks remain separate.

Git can bypass hooks with `git commit --no-verify`; use that only when a commit
must proceed without the formatter check.

<img width="1376" height="985" alt="image" src="https://github.com/user-attachments/assets/fdb1f1fe-3dd3-4f29-a4fd-674e9cc4b270" />
