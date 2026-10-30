# Operate the Stage 7 web runtime

Stage 7 runs inside ordinary chat. Search discovers URLs, reading records page
evidence, and crawling collects a bounded set of related pages. Deep research is
not part of this release. Read [the architecture report](stage-7-web.md) for the
current agent loop, trust boundaries, storage, limits, and recovery behavior.

## Costs and credentials

| Operation               | Dependency                               | What consumes money or resources                |
| ----------------------- | ---------------------------------------- | ----------------------------------------------- |
| Agent reasoning         | Existing OpenAI or Anthropic backend key | Model tokens and provider usage                 |
| Search discovery        | Separate Brave backend key               | Brave search requests under that account's plan |
| HTML, text, PDF reading | Local isolated Docker worker             | CPU, memory, bandwidth, and evidence storage    |
| JavaScript rendering    | Chromium in the worker image             | More CPU and memory, plus bounded HTTP requests |
| Source viewing          | Existing authenticated API and Postgres  | Stored extracted text and API traffic           |

An OpenAI key alone does not configure Brave. Missing search credentials produce
an explicit unavailable result. No fallback search vendor is contacted. The
application reserves each search attempt before the request and shares a monthly
ceiling across users with the same key. The initial ceiling is 1,000 attempts per
UTC month, and the operator can lower it. Provider billing controls remain
authoritative, including usage of that key outside Trellis.

The verification fixtures use fake model/search responses and deterministic
public-network fixtures. They do not make paid OpenAI, Anthropic, or Brave calls.

## Prerequisites

- Use the existing working Trellis authentication and Supabase configuration.
  Use the publishable key and verified user token; never add a service-role key.
- Review and apply the ordered additive evidence migrations to the intended
  development database before enabling tools. This handoff does not apply live
  migrations or deploy the application.
- Docker must run Linux containers and support the kernel egress guard, Unix
  socket volumes, resource limits, seccomp, and Chromium's namespace sandbox.
  Readiness fails closed when these controls cannot be established.
- The supported initial arrangement uses one backend process beside Docker.
  The two HTTP slots and one renderer gate are shared by that process's accounts.
  Durable run budgets and origin pacing remain database-backed.
- Use pinned uv 0.12.5 and the repository's Python 3.14 environment. Keep the
  existing Bun, Turbo, Vite, React, and TypeScript toolchain for the frontend.

## Verify before starting

From `backend/`, select a verified uv 0.12.5 binary. The binary below was used
for this handoff; replace its path after a tool reinstall. The existing backend
README also documents the `uvx --from uv==0.12.5 uv` alternative.

```bash
set -eu
export TRELLIS_UV=/tmp/trellis-uv-bin/uv
[ "$("$TRELLIS_UV" --version | awk '{print $2}')" = 0.12.5 ] || exit 1
"$TRELLIS_UV" sync --locked
make check UV="$TRELLIS_UV"
"$TRELLIS_UV" run --locked python -m scripts.verify_web_isolation
```

The Docker proof builds the exact worker and gateway, exercises actual HTTP/TLS
connections and JavaScript execution, and prints immutable image IDs. It checks
forbidden destinations, DNS/redirect policy, robots, size/decompression limits,
lease expiry, cancellation, sandbox startup, and cleanup. Run one proof per Docker
daemon because its isolated fixture reserves `93.184.216.0/24`. Temporary
containers, networks, volume, certificates, and derived fixture images are removed.
The production images remain available by their printed IDs.

Database verification belongs on a disposable database. The complete CI recipe
is in `.github/workflows/backend-ci.yml`; it replays migrations and runs the SQL
ownership, quota, recovery, clearing, and concurrency fixtures. Its bootstrap
auth schema is a test fixture, not a replacement for Supabase Auth or PostgREST.

## Build and configure a local runtime

Run these commands from `backend/`. Use fresh names and a fresh state directory;
setup uses fail-fast commands and fresh generated Docker names. Stop if any
command fails; do not continue with a previous runtime's variables.

```bash
set -eu
umask 077
export TRELLIS_WEB_STATE_DIR="$PWD/web-runtime.local"
mkdir "$TRELLIS_WEB_STATE_DIR"
chmod 700 "$TRELLIS_WEB_STATE_DIR"
TRELLIS_WEB_RUNTIME_ID="$("$TRELLIS_UV" run --locked python -c 'from uuid import uuid4; print(uuid4().hex)')"
export TRELLIS_WEB_RUNTIME_ID
printf '%s\n' "$TRELLIS_WEB_RUNTIME_ID" > "$TRELLIS_WEB_STATE_DIR/runtime.id"
export TRELLIS_WEB_GATEWAY_CONTAINER="trellis-web-$TRELLIS_WEB_RUNTIME_ID"
export TRELLIS_WEB_GATEWAY_IPC_VOLUME="$TRELLIS_WEB_GATEWAY_CONTAINER-ipc"
export TRELLIS_WEB_NETWORK="$TRELLIS_WEB_GATEWAY_CONTAINER-net"
docker build --iidfile "$TRELLIS_WEB_STATE_DIR/worker.image" -f containers/worker.Dockerfile .
docker build --iidfile "$TRELLIS_WEB_STATE_DIR/gateway.image" -f containers/gateway.Dockerfile .
docker network create "$TRELLIS_WEB_NETWORK"
docker volume create "$TRELLIS_WEB_GATEWAY_IPC_VOLUME"
```

Generate a gateway credential and a private runtime environment file. This prints
no credential. The gateway secret file is readable inside its read-only mount;
the containing host directory and environment file remain private.

```bash
"$TRELLIS_UV" run --locked python - <<'PY'
import json
import os
import secrets
from pathlib import Path

state = Path(os.environ["TRELLIS_WEB_STATE_DIR"]).resolve()
secret_file = state / "gateway-admin"
token = secrets.token_urlsafe(48)
with secret_file.open("x") as stream:
    stream.write(token)
secret_file.chmod(0o444)
values = {
    "TRELLIS_WEB_WORKER_IMAGE": (state / "worker.image").read_text().strip(),
    "TRELLIS_WEB_GATEWAY_IMAGE": (state / "gateway.image").read_text().strip(),
    "TRELLIS_WEB_GATEWAY_CONTAINER": os.environ["TRELLIS_WEB_GATEWAY_CONTAINER"],
    "TRELLIS_WEB_GATEWAY_IPC_VOLUME": os.environ["TRELLIS_WEB_GATEWAY_IPC_VOLUME"],
    "TRELLIS_WEB_GATEWAY_SECRET_FILE": str(secret_file),
    "TRELLIS_WEB_GATEWAY_CONTROL_URL": "http://127.0.0.1:8081",
    "TRELLIS_WEB_GATEWAY_ADMIN_SECRET": token,
}
with (state / "runtime.env").open("x") as stream:
    stream.write("\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n")
(state / "runtime.env").chmod(0o600)
PY
```

Start the gateway with its required boundary. Only its authenticated control
listener is published, on literal host loopback. Website traffic comes from the
gateway, while document workers have networking disabled.

```bash
docker run --detach --name "$TRELLIS_WEB_GATEWAY_CONTAINER" \
  --network "$TRELLIS_WEB_NETWORK" --ipc private --read-only \
  --cap-drop ALL --cap-add NET_ADMIN --cap-add CHOWN \
  --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
  --security-opt no-new-privileges \
  --memory 256m --memory-swap 256m --cpus 1 --pids-limit 64 \
  --shm-size 16m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --publish 127.0.0.1:8081:8081 \
  --mount "type=volume,src=$TRELLIS_WEB_GATEWAY_IPC_VOLUME,dst=/run/trellis-egress" \
  --mount "type=bind,src=$TRELLIS_WEB_STATE_DIR/gateway-admin,dst=/run/secrets/gateway-admin,readonly" \
  "$(cat "$TRELLIS_WEB_STATE_DIR/gateway.image")"
```

The gateway uses temporary startup capabilities only to install its kernel
policy and prepare its socket. It then drops privileges and all capabilities.
The readiness inspector checks the actual kernel policy and running container,
not just the launch arguments. Document workers receive none of these startup
capabilities, no network interface, no Docker socket, and no application secrets.

Run the runtime readiness check using the private environment file:

```bash
"$TRELLIS_UV" run --locked --env-file "$TRELLIS_WEB_STATE_DIR/runtime.env" python -m scripts.check_web_runtime
```

All three results must pass for full reading, crawling, and rendering support.
The check makes no paid search/model request. It does not apply migrations or
replace authenticated per-account database readiness.

Set capabilities deliberately in the existing private backend configuration:

```dotenv
TRELLIS_ENABLED_CAPABILITIES=["web_search","web_read","web_crawl"]
TRELLIS_BRAVE_SEARCH_API_KEY=<operator-owned-backend-key>
TRELLIS_BRAVE_SEARCH_MONTHLY_LIMIT=1000
```

Merge these with any existing capabilities you intentionally enabled. Keep the
model keys and existing Supabase configuration in the private backend environment.
The generated runtime file contains only the worker/gateway configuration.

Start the backend through the same entry point used by the existing application,
supplying the additional runtime environment file. For example:

```bash
"$TRELLIS_UV" run --locked --env-file "$TRELLIS_WEB_STATE_DIR/runtime.env" \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use immutable `sha256:` image IDs. Rebuild images and rerun the proof when updating
the worker, browser, parser, or security profile. Python wheels and the base image
are pinned; apt repositories and browser-download services remain upstream build
inputs. The immutable image ID pins the resulting artifact used at runtime.

## Exercise ordinary chat

1. Search for current public information. Confirm the progress says searching
   and search results remain discovery information until pages are read.
2. Read two supplied public pages and compare them. Open each answer citation and
   inspect the recorded lines and fetch time.
3. Read a public JavaScript page with rendering. Rendering is optional
   `web_read(render=True)`, not a separate research mode.
4. Crawl a small final URL under an explicit path prefix. The initial crawler
   reads HTML, text, and text PDFs; use rendered reading separately for JavaScript.
5. Try an inaccessible page, robots denial, and an internal-network URL. Check
   the recorded failure and stopping reason rather than a fabricated excerpt.
6. Stop a running crawl. Completed evidence remains, consumed budgets remain,
   and the worker and its connections are cleaned up.
7. Clear saved sources after the run finishes or is stopped. Earlier citations
   open a cleared-source tombstone. Existing messages and tool excerpts remain.

Crawl page redirects are blocked to prevent a new target bypassing the robots
decision. Use the final URL as the seed. Robots redirects remain public and
origin-constrained. The crawler keeps a maximum of 200 frontier records, depth
two, and at most 20 charged page attempts shared with the run's other web tools.
The default request asks for five pages and one link level. Recovery reuses
completed records and charges unfinished retries to the same allowance.

## Diagnose unavailable features

| Symptom                                   | Check                                                                                       |
| ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| Search unavailable                        | Brave backend key, supported key format, monthly ceiling, provider account status           |
| Every web tool missing                    | Explicit capabilities and authenticated database schema readiness                           |
| Reading unavailable                       | Pinned image IDs, running gateway, private secret path, socket volume, actual kernel policy |
| Crawl unavailable while reading works     | Worker `public-crawl-v1` protocol and evidence schema version                               |
| Rendering unavailable while reading works | Actual Chromium namespace/Seccomp sandbox probe and private scratch paths                   |
| PDF unsupported                           | Encrypted PDFs and scanned PDFs requiring OCR are outside this release                      |
| Partial evidence                          | Size/text/request limits, blocked browser resources, quota, or retrieval policy outcome     |
| Source lines unavailable                  | Source clearing, retrieval failure, invalid range, or lack of ownership                     |
| Citation unavailable                      | No persisted matching final-answer binding, invalid lines, or an unverified partial answer  |

Never fix readiness by adding `SYS_ADMIN`, disabling Chromium's sandbox or seccomp,
mounting a host browser profile, exposing the Docker socket, or enabling direct
worker networking. A failed boundary means the feature stays unavailable.

## Stop the local runtime

After active runs finish or are stopped, use the same private state directory
and remove only the generated resources recorded there. If starting a new shell,
set `TRELLIS_UV` and `TRELLIS_WEB_STATE_DIR` to the verified binary and that exact
state directory first:

```bash
set -eu
TRELLIS_WEB_RUNTIME_ID="$("$TRELLIS_UV" run --locked python - <<'PYID'
import os
from pathlib import Path
value = (Path(os.environ["TRELLIS_WEB_STATE_DIR"]) / "runtime.id").read_text().strip()
if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
    raise SystemExit("Invalid runtime identity; do not remove Docker resources.")
print(value)
PYID
)"
export TRELLIS_WEB_RUNTIME_ID
export TRELLIS_WEB_GATEWAY_CONTAINER="trellis-web-$TRELLIS_WEB_RUNTIME_ID"
export TRELLIS_WEB_GATEWAY_IPC_VOLUME="$TRELLIS_WEB_GATEWAY_CONTAINER-ipc"
export TRELLIS_WEB_NETWORK="$TRELLIS_WEB_GATEWAY_CONTAINER-net"
docker rm --force "$TRELLIS_WEB_GATEWAY_CONTAINER"
docker volume rm "$TRELLIS_WEB_GATEWAY_IPC_VOLUME"
docker network rm "$TRELLIS_WEB_NETWORK"
```

Worker images and the private local configuration remain for deliberate reuse or
removal. Source evidence lives in the authenticated database, independently of
these containers. Clearing sources is an application action.

## Remaining limits

Public-only egress does not prove confidentiality when a model can read private
context and choose public queries or URLs. Keep queries minimal and avoid private
context in sessions that do not need it. Extracted instructions remain untrusted
data and cannot grant tools or permissions.

Citation validation proves ownership, recorded availability, and line bounds. It
does not prove that every claim follows from the cited text. Publication metadata
is optional; fetch time identifies retrieval time, not publication time.

Controlled binding errors, timeouts, and cancellation preserve paid model usage
without promoting an unsupported answer. A hard process death retains the
existing unknown-model-outcome recovery policy; this stage does not introduce a
new provider exactly-once guarantee.
