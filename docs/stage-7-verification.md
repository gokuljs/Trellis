# Stage 7 implementation and verification handoff

All nine branches are implemented and pushed in dependency order. The user opens
and reviews the PRs, chooses their titles and descriptions, and decides when to
merge. No PR, merge, live migration or production deployment was performed.

The application remains ordinary chat. Web tools join the existing RunService
and ToolRegistry loop. Deep research remains outside this release.

Read the [architecture and security report](stage-7-web.md),
[operator setup guide](stage-7-operations.md), and
[stack review guide](stage-7-stack.md).

## Pushed review stack

Open each PR against its listed base. Review and merge from the first row down.
The first comparison uses remote main at c190672 and contributes only the
architecture commit, excluding pre-existing project work.

| Order | Branch comparison                                                                                                                       | Base                       | Implementation commits |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | ---------------------- |
| 1     | [search-and-crawler](https://github.com/gokuljs/Trellis/compare/main...search-and-crawler)                                              | main                       | 1                      |
| 2     | [search-and-crawler-runtime](https://github.com/gokuljs/Trellis/compare/search-and-crawler...search-and-crawler-runtime)                | search-and-crawler         | 1                      |
| 3     | [search-and-crawler-sources](https://github.com/gokuljs/Trellis/compare/search-and-crawler-runtime...search-and-crawler-sources)        | search-and-crawler-runtime | 2                      |
| 4     | [search-and-crawler-fetch](https://github.com/gokuljs/Trellis/compare/search-and-crawler-sources...search-and-crawler-fetch)            | search-and-crawler-sources | 3                      |
| 5     | [search-and-crawler-reading](https://github.com/gokuljs/Trellis/compare/search-and-crawler-fetch...search-and-crawler-reading)          | search-and-crawler-fetch   | 4                      |
| 6     | [search-and-crawler-browser](https://github.com/gokuljs/Trellis/compare/search-and-crawler-reading...search-and-crawler-browser)        | search-and-crawler-reading | 4                      |
| 7     | [search-and-crawler-crawl](https://github.com/gokuljs/Trellis/compare/search-and-crawler-browser...search-and-crawler-crawl)            | search-and-crawler-browser | 3                      |
| 8     | [search-and-crawler-chat](https://github.com/gokuljs/Trellis/compare/search-and-crawler-crawl...search-and-crawler-chat)                | search-and-crawler-crawl   | 3                      |
| 9     | [search-and-crawler-verification](https://github.com/gokuljs/Trellis/compare/search-and-crawler-chat...search-and-crawler-verification) | search-and-crawler-chat    | 2                      |

The history below ends before this final report commit. Comparison links show
current complete history, including handoff documentation. Branch names have no
codex/ prefix. After a parent merges, keep unmerged descendants aligned before
reviewing the next layer. Squash merges change ancestry: restack carefully,
update PR bases, and preserve other contributors' work.

## Delivered behavior

- web_search provides bounded Brave discovery and persisted shared-key monthly
  attempt accounting. Snippets remain discovery information.
- web_read actually retrieves HTML/text/PDF evidence and optionally renders
  JavaScript in sandboxed Chromium. Accepted text, hashes and lines are immutable.
- web_crawl fixes a public origin/path scope, obeys robots, paces and deduplicates
  requests, limits pages/depth/frontier, and checkpoints evidence atomically.
- web_source reads bounded recorded lines without fetching the website again.
- Normal chat shows progress, safe source views, validated final citations,
  accurate failures, replay, clearing and unavailable-source tombstones.
- Web capabilities do not grant workspace access. Ownership, generation,
  deadline, cancellation and allowances come from trusted runtime context.

## Verification results

Exact staged acceptance and operator units each passed backend make check using
Python 3.14.7 and actual uv 0.12.5: Ruff formatting, Ruff lint, ty,
**592 tests**, and **91.76% configured coverage**. That coverage floor measures
the configured logging modules, not the whole backend.

The frozen frontend passed **80 Bun tests**, lint, TypeScript checks and the
production build with Node 24.13.0 and Bun 1.2.4. The existing large-chunk advisory
remains. This repository has no frontend CI workflow; these checks ran locally
on an independent frozen snapshot.

Two independent browser runs passed all **31 routing and web-evidence checks**:
saved/partial citations, replay, progress, escaped bounded excerpts, failures,
safe links, clearing/tombstones, account/session changes and stale-request
cancellation. Privacy was PASS and unexpected external requests were zero.
Remote Markdown images become explicit links rather than automatic loads.

| Boundary                    | Evidence                                                                                                                     | Result                                              |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Runtime                     | Trusted context, capability, cancellation and recovery regressions                                                           | Passed                                              |
| Real PostgreSQL             | Fresh ordered migrations, seven SQL fixtures, four concurrency suites                                                        | Passed                                              |
| Allowances                  | Concurrent reservations, owner quota, shared search key/month, restart, clearing and recovery                                | Passed                                              |
| Public network              | Actual kernel guard, DNS pinning, mixed/private/metadata rejection, TLS host/SNI and redirects                               | Passed; forbidden targets received zero connections |
| Retrieval                   | Transfer/decode/compression/PDF bounds, deadlines, lease expiry and cancellation                                             | Passed                                              |
| Chromium                    | Actual namespace/Seccomp sandbox, zero capabilities, constrained subrequests, request ceiling and cleanup                    | Passed                                              |
| Crawl                       | Atomic page/link/usage completion, lost replies, scope encoding, robots, pacing and recovery limits                          | Passed                                              |
| Complete normal chat        | Four model steps, four tool dispatches, two real HTTPS reads, recorded lines, two citations, zero workspace access           | Passed                                              |
| Complete bounded crawl chat | Three model steps, two-page/one-level bound, queued unattempted page, no grandchild, source reads and citations              | Passed offline                                      |
| Operator setup              | Fresh private environment, exact Dockerfiles/flags, actual readiness/kernel inspection and unauthenticated-control rejection | Passed                                              |
| Failure guard               | Invalid runtime identity in Bash and Zsh with mocked Docker                                                                  | Passed; zero cleanup calls                          |

The Docker chat proof explicitly labels its evidence store
in_memory_contract_fixture. It combines real isolated retrieval with real
RunService/context/tool flow and fake paid providers. PostgreSQL ownership,
transactions and concurrency are tested separately against real PostgreSQL.
This is not a live Supabase/Auth/model-provider deployment test. No paid OpenAI,
Anthropic or Brave calls were made.

Independent operator readiness exited 0 with exactly one JSON result:

    {"http": true, "crawl": true, "render": true}

It checked the actual read-only gateway, private IPC, loopback binding, resource
limits, UID/GID 10001, zero capability sets, NoNewPrivs, Seccomp mode 2 and the
public-http-v1 kernel policy. Unauthenticated control returned 403. Private file
modes and cleanup were verified without printing or committing credentials.
Temporary runtime resources and private files were removed. Production images
remain available locally:

    worker: sha256:a7ff8ad59d0cfe6962986cb49d40170ec2ac963677873f8c357c9598c369d73f
    gateway: sha256:e21159c4831243c3d969ffe78852425ad90156415b2b7026054f623c29ab1bb6

These are local artifact IDs, not published registry references. Rebuild and
verify on another machine.

## Remote CI evidence

- [Runtime](https://github.com/gokuljs/Trellis/actions/runs/40967600863).
- [Source storage](https://github.com/gokuljs/Trellis/actions/runs/40970292727).
- [Public fetch isolation](https://github.com/gokuljs/Trellis/actions/runs/40974188152).
- [Search and reading](https://github.com/gokuljs/Trellis/actions/runs/40977928746).
- [Browser and Linux sandbox fix](https://github.com/gokuljs/Trellis/actions/runs/40981588427).
- [Bounded crawl](https://github.com/gokuljs/Trellis/actions/runs/40982943175).
- [Chat backend/progress](https://github.com/gokuljs/Trellis/actions/runs/40986172497).
- [Complete chat acceptance](https://github.com/gokuljs/Trellis/actions/runs/40987307942).
- [Final operator code](https://github.com/gokuljs/Trellis/actions/runs/40987711641), passed.

The frontend-only chat commit inherits verified backend code and was checked
locally as above. This final report follows implementation checks; its backend
README links also trigger the existing backend workflow.

## Reproduce

From backend, use the verified pin or the README's uvx equivalent:

    UV_CACHE_DIR=/tmp/trellis-uv-cache make check UV=/tmp/trellis-uv-bin/uv
    UV_CACHE_DIR=/tmp/trellis-uv-cache /tmp/trellis-uv-bin/uv run --locked python -m scripts.verify_web_isolation

Run one Docker proof per daemon: its isolated fixture reserves 93.184.216.0/24.
It removes temporary containers, networks, volume, certificates and derived
images. The seven normal chat/crawl acceptance cases also run in make check.

From frontend with a supported Node version:

    bun test apps/web/tests
    bun run lint
    bun run typecheck
    VITE_API_BASE_URL=https://api.example.test VITE_RUNTIME_WS_URL=wss://api.example.test/api/runtime bun run build
    bunx vite --config apps/web/tests/browser/chat-routing.config.ts

Open the port 3002 fixture and run its routing checks. Evidence must show all
checks passed, privacy PASS and no unexpected external requests. Build origins
above are inert fixtures. The disposable database recipe is in
.github/workflows/backend-ci.yml; its bootstrap belongs only in a test database.
Use the operator guide for application setup. No live migration is implied.

## Operating requirements and remaining limits

Search needs a separate Brave backend key. Model keys pay for model calls
independently. Direct retrieval avoids a crawler vendor's per-page fee but still
uses compute, bandwidth and storage. Provider billing controls remain
authoritative; there is no guaranteed zero-cost service.

Tools require reviewed migrations, explicit capabilities, configuration and
readiness. The supported local arrangement uses one backend process. Durable
budgets and origin pacing remain in the database; HTTP/render gates are shared
by accounts in that process.

The initial crawler handles HTML, text and text PDFs. Use rendered web_read
separately for JavaScript pages. Crawl-page redirects are blocked to prevent
robots bypass; start at the final URL. Authenticated browsing, CAPTCHA bypass,
OCR, deep research and scheduled collection are outside scope. Restricted browser
resources can yield an honest partial source.

Citation processing accepts 50 references and at most 64 KiB UTF-8 when the raw
prefix appears. Plain answers retain existing limits. Unsupported Unicode bare
link markers are rejected conservatively; some normalized indented paragraph
markers remain literal in the frontend. Validation proves owned recorded line
availability, not semantic truth of every claim.

Clearing removes saved source text/metadata and fences late writes. Historical
references become unavailable. Existing messages and tool excerpts remain. The
extracted-text quota is not a complete database-billing cap.

External content cannot grant tools or permissions. Public egress does not prove
confidentiality when a model sees private context and chooses public queries or
URLs. Controlled finalization failures retain paid usage without promotion; hard
process death retains the existing unknown-model-outcome recovery policy.

## Atomic implementation history

### search-and-crawler

- `cfc7133` Document the web search architecture

### search-and-crawler-runtime

- `9a9289f` Add trusted context for run tools

### search-and-crawler-sources

- `40df87b` Persist web sources and crawl checkpoints
- `2f2014e` Add source reading and clearing APIs

### search-and-crawler-fetch

- `337bd4b` Enforce bounded public HTTP retrieval
- `7196549` Reject port zero in public URLs
- `1bc3be8` Isolate retrieval workers behind public egress

### search-and-crawler-reading

- `1713fba` Extract HTML and PDF text in isolated workers
- `0213bf2` Add bounded Brave search discovery
- `abaf481` Persist shared monthly search allowances
- `7f0b03f` Add web search and source tools to chat runs

### search-and-crawler-browser

- `f520c34` Add sandboxed browser page reading
- `17cb084` Prove browser isolation with real page requests
- `e94c154` Report Chromium sandbox startup failures
- `ea45774` Keep Chromium configuration in bounded scratch space

### search-and-crawler-crawl

- `5dec0bf` Enforce robots policies in isolated crawl retrieval
- `9efd4b6` Persist atomic crawl checkpoints and origin pacing
- `176f44b` Add bounded crawling to normal chat runs

### search-and-crawler-chat

- `5a1cb91` Validate saved-source citations before publishing answers
- `eefd701` Report bounded crawl progress through run events
- `45fafb2` Show recorded sources and citations in chat

### search-and-crawler-verification

- `bf23e87` Prove complete web chat and bounded crawl scenarios
- `0740322` Document and verify private local web runtime setup
