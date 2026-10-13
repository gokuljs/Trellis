# Trellis frontend

The Trellis React app restores saved chats from the FastAPI service and uses
relative `/api` URLs. Vite proxies those calls to `http://127.0.0.1:8000` in
development, so no CORS setup is needed.

From this directory:

```bash
bun install --frozen-lockfile
bun run dev
```

Configure provider secrets in the backend, then open `http://127.0.0.1:3000`,
sign in, choose an available model, and start a chat. The web app does not ask
for provider keys or folders. A blank draft is saved when its first message is
sent.

Requires Node.js 22.12 or newer and Bun 1.2.4.

Verification:

```bash
bun run format
bun run format:check
bun run lint
bun run typecheck
bun run test:contracts
bun run build
```
