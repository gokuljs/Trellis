# Trellis frontend

The Trellis React app restores local sessions from the FastAPI service and uses
relative `/api` URLs. Vite proxies those calls to `http://127.0.0.1:8000` in
development, so no CORS setup is needed.

From this directory:

```bash
bun install --frozen-lockfile
bun run dev
```

Open `http://127.0.0.1:3000`, add a provider key in Settings, and start a new
session. A blank draft is only persisted when its first message is sent.

Requires Node.js 20.19 or newer and Bun 1.2.4.

Verification:

```bash
bun run test
bun run lint
bun run typecheck
bun run build
```
