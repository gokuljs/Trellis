# Trellis

Trellis is a self-improving multi-agent system for coding and research. It coordinates specialized agents that can work together, learn from previous tasks, and continuously improve how they solve problems.

## Local chat quick start

Install dependencies once, then start both services from the repository root:

```bash
uvx --from uv==0.12.5 uv sync --locked --directory backend
bun install --cwd frontend --frozen-lockfile
make dev
```

The frontend is served at `http://127.0.0.1:3000` and the backend at
`http://127.0.0.1:8000`. The command uses plain streamed logs so both services
remain readable in one terminal. Press `Ctrl-C` once to stop both services.

Then open `http://127.0.0.1:3000`, visit Settings, and add an OpenAI or
Anthropic key. Trellis creates a stable local installation ID automatically and
restores saved sessions from `~/.trellis` after restarts. Set
`TRELLIS_DATA_DIR` before starting the backend to store local data elsewhere.

<img width="1376" height="985" alt="image" src="https://github.com/user-attachments/assets/fdb1f1fe-3dd3-4f29-a4fd-674e9cc4b270" />

