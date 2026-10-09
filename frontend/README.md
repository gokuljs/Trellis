# Trellis frontend

The Trellis React app requires Google or GitHub sign-in through Supabase Auth
before opening the local workspace. Missing configuration keeps the workspace
locked and shows setup instructions.

Requires Node.js 22.12 or newer and Bun 1.2.4. The Supabase SDK requires Node.js
22; the minimum also satisfies Vite 8's supported Node.js 22 range.

## Local environment

From this directory:

```bash
bun install --frozen-lockfile
cp apps/web/.env.example apps/web/.env.local
```

Replace both placeholders in `apps/web/.env.local` with the URL and publishable
key from your existing Supabase project's Connect dialog:

```dotenv
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_<your-key>
```

The committed `.env.example` contains placeholders; real `.env.local` files are
ignored by Git. Vite exposes `VITE_` values to the browser, so use only the
[publishable key](https://supabase.com/docs/guides/getting-started/api-keys).
Never put a Supabase secret key, `service_role` key, or Google/GitHub client
secret in a `VITE_` variable, frontend source, or committed environment file.
Provider client secrets belong only in Supabase's provider settings.

Restart Vite after changing environment values. Configure the project below,
then run `bun run dev`, or use `make dev` from the repository root to start both
services.

## Supabase project and providers

In the existing project's Authentication settings, enable **Allow new users to
sign up**, then enable Google and GitHub under **Sign In / Providers**. This
permits first-time OAuth sign-up as well as returning-user sign-in. See
[Supabase's configuration guide](https://supabase.com/docs/guides/auth/general-configuration).

Under **URL Configuration**, set:

| Setting                 | Value                    |
| ----------------------- | ------------------------ |
| Site URL                | `http://127.0.0.1:3000`  |
| Redirect URL            | `http://127.0.0.1:3000/` |
| Additional redirect URL | `http://localhost:3000/` |

Trellis returns to the root of the origin where sign-in started. Include the
trailing slash in these exact [redirect allowlist entries](https://supabase.com/docs/guides/auth/redirect-urls).
For a future deployment, configure its actual HTTPS origin and exact redirect
URL before deploying.

Both providers must send their OAuth response to the **Supabase callback**:
`https://<project-ref>.supabase.co/auth/v1/callback`. Copy the exact callback
from the provider settings. This differs from the app redirect above: the
provider returns to Supabase, which then returns to Trellis.

- **Google:** Configure Google Cloud's OAuth consent screen and a **Web
  application** OAuth client. Add `http://127.0.0.1:3000` and
  `http://localhost:3000` as authorized JavaScript origins and the Supabase
  callback as an authorized redirect URI. Use only `openid`, email, and profile
  scopes. Save the client ID and secret in Supabase's Google provider settings.
  Follow the [Google provider guide](https://supabase.com/docs/guides/auth/social-login/auth-google).
  For an External app in Testing, add intended accounts under **Audience →
  Test users**; Google's audience restrictions can otherwise block testers. See
  [Google's consent setup](https://developers.google.com/workspace/guides/configure-oauth-consent).
- **GitHub:** Register an **OAuth App** with homepage
  `http://127.0.0.1:3000` and the Supabase callback as its authorization callback
  URL. Save its client ID and secret in Supabase's GitHub provider settings.
  Follow the [GitHub provider guide](https://supabase.com/docs/guides/auth/social-login/auth-github).
  Keep permissions limited to identity and email; Trellis requests no repository
  scopes. GitHub documents the available [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps).

## Account storage and local workspace

Supabase manages account records in `auth.users` and provider identities in
`auth.identities`, including the email and profile metadata returned by the
provider. This integration adds no custom user table or database migration.
The Auth schema is not exposed through Supabase's generated data API; inspect
records in **Authentication → Users**. See [user management](https://supabase.com/docs/guides/auth/managing-user-data)
and [identities](https://supabase.com/docs/guides/auth/identities).

Sign-in uses [PKCE](https://supabase.com/docs/guides/auth/sessions/pkce-flow).
Complete it in the same browser and device, using the same origin; do not move
the callback between `localhost` and `127.0.0.1`. The SDK saves the session in
browser storage and restores it after refresh. **Sign out** ends the current
browser session; it does not delete the account or local workspace.

The cloud account is separate from the existing local installation profile,
chats, settings, and model keys. The app still uses relative `/api` URLs, with
Vite proxying to `http://127.0.0.1:8000`; backend APIs are unchanged. Backend
authorization and isolation of application data per account are deferred to a
later PR. Accounts using the same local installation share that installation's
data; the frontend sign-in gate does not authorize direct API requests.

After signing in, complete the existing local onboarding, add a model provider
key in Settings, and start a session. A blank draft is persisted only when its
first message is sent.

## Verification

With real project values and configured providers, manually check:

1. In a signed-out browser, the sign-in screen appears and no `/api` requests
   start before authentication.
2. Sign in with a new Google account and a new GitHub account. For each, check
   **Authentication → Users** for its saved record and provider identity.
3. Sign out and repeat with existing accounts. Refresh after signing in and
   confirm the session restores. Confirm sign-out returns to the sign-in screen
   and remains signed out after refresh.
4. Cancel provider consent, return to Trellis, and confirm a retry is available.
   Complete one flow at a time in the browser where it started.

Placeholder values do not activate OAuth; live provider sign-in must be verified
after configuring the real project and credentials.

Automated checks from this directory:

```bash
bun run format:check
bun run test
bun run lint
bun run typecheck
bun run build
```
