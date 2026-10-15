# Trellis frontend

The Trellis React app restores saved chats from the FastAPI service and uses
relative `/api` URLs. Vite proxies those calls to `http://127.0.0.1:8000` in
development, so no CORS setup is needed.

From this directory:

```bash
bun install --frozen-lockfile
cp .env.example .env
bun run dev
```

Before starting Vite, set `VITE_SUPABASE_URL` and
`VITE_SUPABASE_PUBLISHABLE_KEY` in `frontend/.env` for the same Supabase project
as the backend. These browser values must use the publishable key.

Configure provider secrets in the backend, then open `http://127.0.0.1:3000`,
sign in, choose an available model, and start a chat. The web app does not ask
for provider keys or folders. A blank draft is saved when its first message is
sent.

Requires Node.js 22.12 or newer and Bun 1.2.4.

See [Stage 2 setup and guarantees](../docs/stage-2.md) for model discovery,
streaming recovery, context compaction, and history limits.

Verification:

```bash
bun run format
bun run format:check
bun run lint
bun run typecheck
bun run build
```
