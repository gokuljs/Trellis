# Frontend Sentry integration design

Trellis reports unexpected frontend failures and sampled performance data to
Sentry. This design preserves the React 19, Vite 8, Bun, Turbo, and TanStack Query
toolchain. Backend instrumentation is outside this change.

## Capture failures after retries and recovery

Initialize the SDK before application imports. React 19 root error hooks own
render-error capture. A native React boundary displays a reload fallback without
capturing the error a second time. Recoverable React errors have warning severity.

TanStack Query cache callbacks own terminal query and mutation failures. Direct
application operations report only at their final failure handlers. The REST
client, notification helper, and WebSocket reconnect machinery do not report
exceptions themselves.

Runtime recovery has two layers: the WebSocket client reconnects, and the app can
then restore the accepted run from saved history. Report an outage after those
steps fail, including exhaustion branches that return without throwing. Preserve
account, session, and run-generation ownership checks before reporting.

Ignore deliberate aborts, stale work, expired credentials, ordinary validation
failures, and known cancellation or budget outcomes. Final service outages,
timeouts, protocol faults, and unexpected errors remain actionable. Suppress the
same error object reaching multiple reporting owners without merging distinct
failures by message or time window.

## Keep conversations and credentials local

The context-preview query key contains the complete draft message. Other query
keys contain session, turn, and run identifiers. Runtime authentication sends a
token in a JSON-RPC frame, and run requests contain prompts. Error messages from
the service can also contain arbitrary text.

Callers therefore provide only an error and a fixed operation label. Never attach
query keys, mutation variables, application state, RPC frames, or runtime events.
The telemetry policy replaces free-form exception messages with safe
classifications while preserving exception types and source-mapped stacks.

Sentry 11 uses rich data-collection defaults and streamed spans. Explicitly disable
identity, headers, cookies, bodies, URL parameters, AI content, and local-variable
collection. Remove console and DOM breadcrumb collectors. Apply outgoing filters
to errors and streamed spans, normalize dynamic API paths, and strip query strings
and fragments before transmission. OAuth callback cleanup happens after SDK
startup, so it cannot replace the outgoing URL policy.

Session Replay, profiling, user feedback, logs, application metrics, and user
identity collection are disabled. Reduced diagnostic text is an accepted tradeoff
for keeping chat content and credentials out of telemetry.

## Trace bounded frontend operations

Capture errors at 100% and traces at 10% by default. Browser tracing measures page
loads, URL navigation, and Trellis API requests. Propagate trace headers only to
same-origin `/api` requests, excluding Supabase and OAuth services.

WebSocket performance needs explicit spans. Measure the bounded acknowledgements
for `run.start`, `run.subscribe`, `run.cancel`, and `run.respond`. End every span on
success, rejection, timeout, or teardown. Do not span the full model stream or
attach RPC arguments and results.

## Separate ordinary builds from source-map uploads

Production reporting requires a valid DSN and production configuration. Local and
custom preview modes require explicit opt-in. A false enabled setting disables
initialization everywhere. Missing configuration must not stop the app starting.

Ordinary builds require no Sentry credentials and produce no source maps. The
explicit `build:sentry` command requires an organization, project, build token, and
release. It forces the web build so a Turbo cache hit cannot skip uploading maps.

Place the Sentry Vite plugin after React and Tailwind. Generate hidden maps, upload
them, and delete maps only under the web output directory. Missing configuration
or an upload failure fails the upload build. Public release configuration is
shared by the SDK and plugin. Build tokens never use a `VITE_` prefix.

Preserve ordinary Turbo caching. Hash public configuration and upload settings;
pass the token through only to build processes. Disable the plugin's own telemetry,
automatic commit association, and deployment creation.

## Review the implementation in order

Each unit includes its focused verification and is pushed before the next starts.
Published commits are not rewritten.

1. Document the frontend Sentry integration design.
2. Add Sentry frontend dependencies.
3. Add frontend telemetry configuration and privacy controls.
4. Capture React failures and browser performance.
5. Report frontend request failures after retries.
6. Instrument runtime requests and exhausted recovery.
7. Upload private frontend source maps.
8. Document Sentry setup and verification.

The blocking work is configuration and privacy policy. Startup, request failures,
runtime operations, and build uploads follow as separately verifiable units.
Independent design exploration is read-only. A single implementation owner changes
shared files and the lockfile; commit and push operations are serialized.

Use Bun tests with a recording Sentry transport to inspect actual envelopes.
Exercise hostile payloads, activation settings, retries, observer deduplication,
cancellation, runtime reconciliation, and bounded span settlement. Check formatting,
lint, types, and a fresh production build. Inspect the fallback in a browser.
Live Sentry ingestion and symbolication require a configured project and remain
separate from local evidence.

## Research sources

The selected direct dependencies are `@sentry/react@11.8.0` and
`@sentry/vite-plugin@11.8.0`, checked against their published package metadata.
The React SDK supports React 19 and the repository's supported Node versions.

- [React setup and React 19 hooks](https://docs.sentry.io/platforms/javascript/guides/react/)
- [SDK options, data collection, and streamed spans](https://docs.sentry.io/platforms/javascript/guides/react/configuration/options/)
- [Vite source-map uploads](https://docs.sentry.io/platforms/javascript/guides/react/sourcemaps/uploading/vite/)
- [Vite plugin package](https://www.npmjs.com/package/@sentry/vite-plugin/v/11.8.0)
- [Turbo environment handling](https://turborepo.com/docs/crafting-your-repository/using-environment-variables)
- [TanStack QueryCache callbacks](https://tanstack.com/query/latest/docs/reference/QueryCache)
- [TanStack MutationCache callbacks](https://tanstack.com/query/latest/docs/reference/MutationCache)
