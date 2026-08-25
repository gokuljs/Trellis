# Trellis

Trellis is a self-improving multi-agent system for coding and research. It coordinates specialized agents that can work together, learn from previous tasks, and continuously improve how they solve problems.

## Local chat quick start

Run the backend and frontend in separate terminals:

```bash
cd backend
uvx --from uv==0.12.5 uv sync --locked
uvx --from uv==0.12.5 uv run fastapi dev
```

```bash
cd frontend
bun install --frozen-lockfile
bun run dev
```

Then open `http://127.0.0.1:3000`, visit Settings, and add an OpenAI or
Anthropic key. Trellis creates a stable local installation ID automatically and
restores saved sessions from `~/.trellis` after restarts. Set
`TRELLIS_DATA_DIR` before starting the backend to store local data elsewhere.

<img width="1839" height="624" alt="image" src="https://github.com/user-attachments/assets/4293e622-379f-4b4d-893f-53a078872ba0" />
<img width="1437" height="909" alt="image" src="https://github.com/user-attachments/assets/dc59e124-e482-44fa-8e53-301f2e7891ed" />



