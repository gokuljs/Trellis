# Trellis

Trellis is a self-improving multi-agent system for coding and research. It coordinates specialized agents that can work together, learn from previous tasks, and continuously improve how they solve problems.

## Local chat quick start

Install dependencies once, then start both services from the repository root:

```bash
uvx --from uv==0.12.5 uv sync --locked --directory backend
bun install --cwd frontend --frozen-lockfile
bun run --cwd frontend dev:all
```

The frontend is served at `http://127.0.0.1:3000` and the backend at
`http://127.0.0.1:8000`. Press `Ctrl-C` once to stop both services.

Then open `http://127.0.0.1:3000`, visit Settings, and add an OpenAI or
Anthropic key. Trellis creates a stable local installation ID automatically and
restores saved sessions from `~/.trellis` after restarts. Set
`TRELLIS_DATA_DIR` before starting the backend to store local data elsewhere.

<img width="1839" height="624" alt="image" src="https://github.com/user-attachments/assets/4293e622-379f-4b4d-893f-53a078872ba0" />
<img width="1437" height="909" alt="image" src="https://github.com/user-attachments/assets/dc59e124-e482-44fa-8e53-301f2e7891ed" />
