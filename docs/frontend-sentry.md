# Configure and verify frontend Sentry

Trellis includes `@sentry/react@11.8.0` and `@sentry/vite-plugin@11.8.0`.
Configure a Sentry project to collect unexpected frontend errors and sampled
performance data. Backend instrumentation is outside this integration.

Read the [integration design and research](frontend-sentry-design.md) for the
original decisions and the eight-commit review sequence.

## Create the frontend project

1. Open your Sentry organization and create a project with the React platform.
   Choose a project name, such as `trellis-frontend`, and assign its team.
2. Record the organization and project slugs. Build uploads use those slugs,
   rather than the project display name or numeric project ID.
3. Open the project's **Client Keys (DSN)** settings and copy its public DSN.
   Sentry also displays the DSN in the project setup snippet.
4. Put the DSN in `VITE_SENTRY_DSN` in `frontend/.env` or your frontend build
   environment. The DSN is browser configuration. It is separate from the build
   authentication token.
5. Keep the existing SDK initialization. The repository already contains the
   React hooks, tracing, and source-map plugin. Do not rerun a setup wizard or add
   a second `Sentry.init()` call.

Sentry documents project keys in its
[DSN explanation](https://github.com/getsentry/sentry-docs/blob/master/docs/concepts/key-terms/dsn-explainer.mdx).

## Configure browser reporting

Vite reads environment files from `frontend/`, including mode-specific files.
Environment variables supplied by the build process take precedence. Only
variables prefixed with `VITE_` enter the browser configuration.

| Variable                         | Meaning                                           | Default and validation                                                                                                                                                                                          |
| -------------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VITE_SENTRY_DSN`                | Public DSN for the frontend project.              | Missing or invalid disables initialization without preventing startup. Use the public DSN without a secret, query, or fragment.                                                                                 |
| `VITE_SENTRY_ENABLED`            | Explicit reporting switch.                        | Trimmed `true` enables reporting with a valid DSN. `false` or another nonempty value disables it. Blank or unset uses the production gate below.                                                                |
| `VITE_SENTRY_ENVIRONMENT`        | Sentry environment name.                          | Defaults to Vite's mode. Valid names contain 1 to 64 letters, digits, underscores, periods, or hyphens. Invalid names use the mode as the reported label and disable automatic activation. Correct the setting. |
| `VITE_SENTRY_RELEASE`            | Release shared by browser events and map uploads. | Optional for ordinary builds. Use the full Git commit SHA for upload builds. Invalid browser values are omitted.                                                                                                |
| `VITE_SENTRY_TRACES_SAMPLE_RATE` | Fraction of traces to sample.                     | `0.1`. Accepts a finite number from `0` to `1`. Missing, blank, or invalid values use `0.1`.                                                                                                                    |

With a valid DSN and no explicit switch, reporting requires all three conditions:

- Vite sets `import.meta.env.PROD` to `true`.
- Vite's mode is `production`.
- `VITE_SENTRY_ENVIRONMENT` is unset, blank, or exactly `production` after trimming.

A regular production build therefore reports when configured. Development,
`vite build --mode staging`, and a custom Sentry environment need explicit
`VITE_SENTRY_ENABLED=true`. Setting `VITE_SENTRY_ENABLED=false` disables reporting
in every mode.

These values are compiled into the frontend. Serving an existing production
artifact with `vite preview` retains its production settings. Changing the
preview server's environment does not change that artifact. Rebuild after changing
build configuration. Restart Vite and reload the page after changing development
configuration.

Reportable errors have a sample rate of `1`, or 100%. Traces default to 10%.
Sentry project filters and quotas can still affect ingestion. Session Replay,
profiling, logs, application metrics, user identity, and breadcrumbs are disabled.

## Configure build authentication

Create an organization authentication token for CI. Store it as the protected
`SENTRY_AUTH_TOKEN` secret in the environment that builds the frontend. Avoid
committing the token, putting it in browser configuration, or printing it in build
logs.

Sentry recommends organization tokens because they are independent of individual
user accounts. Its current permissions reference identifies `org:ci` for CI
workflows, including source-map uploads. Use the organization's CI token workflow
with that scope. If your organization manages tokens through an internal
integration, follow Sentry's token guide and the permissions available to your
organization. See [permissions and scopes](https://docs.sentry.io/api/permissions/)
and [creating an organization token](https://docs.sentry.io/api/guides/create-auth-token/).

The upload build reads these additional settings:

| Variable                   | Purpose                                                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SENTRY_UPLOAD_SOURCEMAPS` | Explicit upload opt-in. `build:sentry` sets this to `true`. Blank, unset, or `false` keeps ordinary builds unchanged. Other values fail a build. |
| `SENTRY_ORG`               | Required organization slug for uploads.                                                                                                          |
| `SENTRY_PROJECT`           | Required frontend project slug for uploads.                                                                                                      |
| `SENTRY_AUTH_TOKEN`        | Required build secret. Turbo passes this to build processes without including the value in its environment cache key.                            |
| `VITE_SENTRY_RELEASE`      | Required public release identifier for uploads and browser events.                                                                               |
| `NODE_ENV`                 | Upload builds reject `development`, because the vendor plugin skips uploads in that environment.                                                 |

Organization and project slugs accept letters, digits, underscores, and hyphens.
Upload release identifiers accept 1 to 200 letters, digits, underscores, periods,
`@`, `+`, and `-`. Empty releases, `.`, `..`, and `latest` fail validation. A full
Git SHA satisfies these rules.

Never create a `VITE_SENTRY_AUTH_TOKEN` variable. The build helper uses the token
only in plugin options and does not add a browser `define` value.

## Build and upload one artifact

Run these commands from `frontend/` after CI has injected `SENTRY_AUTH_TOKEN`.
Replace the DSN and slugs with your project values:

```bash
export NODE_ENV=production
export VITE_SENTRY_DSN="https://PUBLIC_KEY@INGEST_HOST/PROJECT_ID"
export VITE_SENTRY_ENVIRONMENT=production
export VITE_SENTRY_RELEASE="$(git rev-parse HEAD)"
export SENTRY_ORG="your-organization-slug"
export SENTRY_PROJECT="your-frontend-project-slug"
bun run build:sentry
```

The command runs
`SENTRY_UPLOAD_SOURCEMAPS=true turbo build --filter=@trellis/web --force`.
The forced web build prevents a Turbo cache hit from skipping the upload side
effect. Ordinary `bun run build` retains normal caching and needs no Sentry
credentials. Turbo includes public Sentry settings and the upload flag, organization,
and project in build cache inputs. The build token is passed through separately.

The upload build does the following:

1. Validate the upload configuration before bundling.
2. Add the Sentry plugin after the existing React and Tailwind plugins.
3. Generate hidden maps and stamp Debug IDs into the build artifacts.
4. Upload JavaScript and maps under `frontend/apps/web/dist/` to the configured
   project, using the same `VITE_SENTRY_RELEASE` as the SDK.
5. Delete `.map` files only under that output directory after the upload step.

The plugin disables its own telemetry, automatic commit association, and deployment
creation. It also disables release injection, release creation, and finalization.
The SDK uses the explicit public release value. Sentry can create the release when
it receives an event with that release.

Missing configuration and upload errors fail the build. Do not deploy a failed
build. After a successful upload build, deploy that exact `apps/web/dist/`
artifact. A later ordinary rebuild produces different artifacts and does not
upload its maps.

Ordinary builds produce no source maps. Upload builds use hidden maps, so public
JavaScript does not advertise a `sourceMappingURL`. Confirm that your final
deployment artifact contains no `.map` files before publishing it. No live upload
has been verified for this repository without a configured Sentry account.

## Understand which failures become issues

[`src/instrumentation.ts`](../frontend/apps/web/src/instrumentation.ts) is the first
entrypoint import. SDK initialization is conditional and does not create a second
client during development reloads.

React 19 root hooks own render-error reporting. The native root boundary displays
the reload fallback and does not capture a second event. Recoverable React errors
have warning severity.

TanStack Query cache callbacks report once after the existing retry policy is
exhausted. Metadata contains a fixed operation label and follows a cache or caller
ownership policy. Cache ownership is the default. It never contains query keys,
drafts, or mutation variables.

Ownership remains stable for each query key:

| Request                                                                                | Reporting owner                                                                                     |
| -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Settings, session pages, context preview, saved run events, and per-turn run summaries | Query cache after terminal failure.                                                                 |
| Session history and session-wide run summaries                                         | Guarded application caller. Recovery can retry or reconcile without creating an intermediate issue. |
| Session model changes                                                                  | Caller after the model-selection ownership check.                                                   |
| Direct workspace, onboarding, and settings operations                                  | Final caller after its controller or mounted-component check.                                       |
| Runtime operations                                                                     | Final caller after reconnect and reconciliation finish.                                             |

Disposing an account's query client suppresses later cache reporting. Application
handlers also check account lifetime, session selection, or run generation before
reporting. Error-object identity prevents a cache callback and an outer handler
from reporting the same failure twice. Distinct error objects remain distinct.

Expired authentication, ordinary validation and quota responses, deliberate
cancellation, expected budget limits, and unavailable provider configuration are
filtered. Service outages, timeouts, protocol errors, and unexpected failures
remain actionable. The REST transport, WebSocket reconnect machinery, and toast
helper do not capture exceptions themselves.

Runtime recovery has a WebSocket reconnect layer and an application reconciliation
layer. Successful recovery stays quiet. A saved completed, cancelled, or interrupted
run with successfully loaded history also stays quiet. Exhausted discovery or
recovery reports at the final guarded handler, including branches that return
without throwing. Historical failed runs do not create new issues when opened.
Quiet refers to error issues. Sampled traces can still contain spans for failed
attempts during a successful recovery.

## Understand tracing and privacy

Browser tracing measures page loads, navigation, and same-origin `/api` requests.
Trace propagation headers are restricted to those API requests. Supabase and
other external requests receive no Sentry trace headers.

WebSocket acknowledgement spans use fixed names `run.start`, `run.subscribe`,
`run.cancel`, and `run.respond`, with the `rpc.client` operation. The wire method
for resuming remains `run.resume`. Only its telemetry name is `run.subscribe`.
Spans start after authentication and finish when the acknowledgement is validated
or the request ends through rejection, timeout, or teardown. They do not span the
model stream and contain no RPC arguments, results, or credentials.

The outgoing privacy policy reconstructs permitted fields instead of forwarding
application objects. It removes identities, cookies, headers, bodies, URL queries
and fragments, AI content, local variables, source context, custom tags, and extras.
It also sanitizes linked exceptions, span links, inherited sampling data, and
envelope headers. Attachments and non-error, non-span envelope items are discarded.

Error messages become fixed classifications such as `service_unavailable` or
`runtime_protocol_error`. Static operation tags identify the failed action, such
as `settings.load`, `context.preview`, or `run.restore`. API identifiers become
templates such as `/api/sessions/:id/runs/:id/events`. Unknown routes become generic
paths. Standard and Trellis error types, permitted source filenames, line and
column positions, trace IDs, and source-map Debug IDs remain available.

This policy trades free-form diagnostic text for source-mapped application stacks
and stable operation labels. Do not add query keys, prompts, profile objects,
settings, or raw runtime events to reports when extending this integration.

Recorded builds around foundation commit `1e0e8e1` and startup commit `bed2611`
reported main JavaScript gzip sizes of 259.20 KB and 316.91 KB, respectively.
The startup integration added about 58 KB in that comparison. Dependencies and
bundling settings change that number.

## Verify local behavior without Sentry traffic

Use the existing frontend toolchain. Verification used Bun 1.2.4 and Node.js
24.13.0. The frontend requires Node.js 22.12 or newer.

Run these commands from `frontend/`:

```bash
bun install --frozen-lockfile
bun test apps/web/tests
bun run format:check
bun run lint
bun run typecheck
bun run build
```

The tests record actual SDK envelopes locally. They cover activation, hostile
payloads, linked exceptions, sampling headers, query retries and shared observers,
caller ownership, runtime acknowledgements, cancellation, timeouts, and builds.
The build tests use Node subprocesses for Vite configuration, preserving the pinned
Bun runner. An isolated ordinary build checks that a dummy build token and public
source maps are absent.

To inspect the actual React fallback and browser instrumentation, start a separate
Vite instance from `frontend/apps/web/`:

```bash
bunx vite --host 127.0.0.1 --port 3017
```

Open `http://127.0.0.1:3017/tests/browser/telemetry.html` and click each button once:

1. **Trigger render failure** displays the application reload fallback and records
   one error.
2. **Trigger uncaught failure** records a second error and unmounts that test root.
3. **Trigger recoverable warning** creates a real hydration mismatch and records
   one warning.
4. **Create API span** exercises fixture requests and checks that only the API
   request receives trace headers.

The final verification panel reports two errors, one warning, and `PASS` for
privacy, capture counts, inherited baggage, API propagation, and external
propagation. Expand **Sanitized envelopes** to inspect the payloads. The fixture
records transport data in memory and supplies local fixture HTTP responses.
It does not send events to Sentry or require a backend. **Reload Trellis** reloads
the page and resets the fixture.

The test HTML is a separate development entrypoint. It is not part of the normal
production build or a product debug route.

## Verify the configured Sentry project

These steps require a real DSN, build token, uploaded maps, and a working signed-in
test account. They have not been run against a live Sentry project here.
Use a controlled deployment and test project. Explicitly enable telemetry if its
mode or Sentry environment differs from production.

1. Build with `build:sentry` and deploy the exact successful output. Confirm that
   the SDK release and upload release use the same Git SHA.
2. Open the deployed app and sign in with a test account. Open browser developer
   tools and the Network request-blocking controls.
3. Add a blocking pattern for that application's `/api/settings` URL only, such as
   `https://YOUR_APP_HOST/api/settings*`. Keep the Sentry ingestion host unblocked.
4. Reload the app. The settings read fails, exhausts its existing retry, and
   displays the workspace failure state. Remove the blocking rule after the check.
5. In the frontend Sentry project's Issues view, filter by the release and
   environment. Inspect the resulting `service_unavailable` error event and its
   `telemetry.operation=settings.load` tag. The message intentionally excludes the
   raw network or service text.
6. Confirm that the event includes the expected release and readable original
   application source frames. Minified bundle positions alone do not establish
   successful symbolication. Check that the matching Debug ID artifacts exist in
   the project's source-map settings if mapping fails.
7. To make trace verification deterministic, temporarily build with
   `VITE_SENTRY_TRACES_SAMPLE_RATE=1`, upload the maps, and deploy that exact output.
   Reload the app and make a normal API request. Inspect page-load and API spans
   in Sentry's trace views. A backend trace is not expected from this frontend-only
   integration.
8. Inspect error and span payloads for absent drafts, tokens, user identity,
   request bodies, breadcrumbs, Replay data, and logs. Confirm that browser
   Supabase requests have neither `sentry-trace` nor `baggage` headers.
9. Restore the trace sample rate to `0.1` or remove the temporary override, then
   rebuild, upload, and deploy the final artifact.

Use the real application for this dashboard check. The local browser fixture
always uses its in-memory transport, regardless of your configured DSN.

## Troubleshoot missing events and maps

| Symptom                                                         | Check                                                                                                                            |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| No events during local development                              | A valid DSN and explicit `VITE_SENTRY_ENABLED=true` are required. Restart Vite and reload after changes.                         |
| No events in a custom or staging build                          | Set the explicit enabled switch. Automatic reporting requires both production mode and the production Sentry environment.        |
| A preview reports production events                             | Its artifact retains the build's configuration. Rebuild with disabled reporting or a deliberately enabled test environment.      |
| An expected validation or cancellation failure creates no issue | The failure policy filters those outcomes. Use a terminal service outage to verify issue delivery.                               |
| Events do not reach the dashboard                               | Inspect requests to the DSN ingestion host, browser blocking or content policy, project filters, quota, and environment filters. |
| Some traces are missing                                         | The default sample rate is 10%. Use a temporary rate of `1` for verification, then restore it.                                   |
| Stack frames remain minified                                    | Confirm a successful upload, the correct project, matching release and Debug IDs, and deployment of the uploaded artifact.       |
| Upload fails before bundling                                    | Supply all required settings, use a valid Git SHA release, and avoid `NODE_ENV=development`.                                     |
| Upload authentication fails                                     | Check the organization CI token, `org:ci` permissions, and the organization and project slugs. Keep the token in CI secrets.     |
| An upload build has been cached                                 | Use `bun run build:sentry`, which forces the web build. A normal cached build cannot repeat upload side effects.                 |
| An issue has less diagnostic text                               | Canonical messages and strict field removal are intentional. Use the fixed operation and source-mapped stack.                    |

## Recorded verification

Local verification on October 16, 2026 passed all 44 frontend tests and the frontend
format, lint, type, and production-build checks. The production build retains its
existing large-chunk warning.

The browser fixture produced exactly two errors and one recoverable warning.
Privacy, inherited baggage, API propagation, external propagation, and reload
fallback checks passed. The normal signed-out application remained unchanged.

Runtime tests execute the actual WebSocket client with fake sockets, authentication,
and deadlines, and inspect SDK-produced envelopes. AppShell's authenticated
recovery closures received source review and focused policy checks. A complete
authenticated application recovery exercise was not run.

Live source-map uploads, Sentry ingestion, symbolication, and account-specific
dashboard checks remain unverified until you configure the project and run the
procedure above.
