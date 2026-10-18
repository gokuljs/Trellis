import {
  afterAll,
  afterEach,
  beforeAll,
  expect,
  setDefaultTimeout,
  test,
} from "bun:test"
import { mkdir } from "node:fs/promises"
import path from "node:path"
import {
  chromium,
  type Browser,
  type BrowserContext,
  type Page,
} from "playwright"

setDefaultTimeout(45_000)

const frontend = path.resolve(import.meta.dirname, "../..")
const backend =
  process.env.TRELLIS_TEST_BACKEND_DIR ?? path.resolve(frontend, "../backend")
const python = path.resolve(backend, ".venv/bin/python")
const artifacts =
  process.env.TRELLIS_TEST_ARTIFACT_DIR ??
  path.resolve(frontend, "test-results/durable")
const origin = "http://127.0.0.1:3039"
const environment = {
  ...process.env,
  TRELLIS_TEST_PROVIDER_DELAY: "2",
  TRELLIS_TEST_PROVIDER_CHUNK_DELAY: "3",
}

type Process = ReturnType<typeof Bun.spawn>
let api: Process | null = null
let vite: Process | null = null
let browser: Browser
let workers: Process[] = []
let nextProcess = 0
const processPrefix = Date.now().toString(36)

function start(command: string[], cwd = backend): Process {
  const name = `process-${processPrefix}-${nextProcess++}`
  return Bun.spawn(command, {
    cwd,
    env: environment,
    stdout: Bun.file(path.join(artifacts, `${name}.stdout.log`)),
    stderr: Bun.file(path.join(artifacts, `${name}.stderr.log`)),
  })
}

async function stop(child: Process | null) {
  if (!child || child.exitCode !== null) return
  child.kill("SIGTERM")
  const timer = setTimeout(() => child.kill("SIGKILL"), 5_000)
  try {
    await child.exited
  } finally {
    clearTimeout(timer)
  }
}

async function until(check: () => Promise<boolean>, timeout = 20_000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (await check()) return
    await Bun.sleep(100)
  }
  throw new Error("Timed out waiting for the durable browser scenario")
}

async function startApi() {
  api = start([
    python,
    "-m",
    "uvicorn",
    "tests.durable_harness:create_api",
    "--factory",
    "--host",
    "127.0.0.1",
    "--port",
    "8123",
  ])
  await until(async () => {
    try {
      return (await fetch("http://127.0.0.1:8123/openapi.json")).ok
    } catch {
      return false
    }
  })
}

async function context(): Promise<BrowserContext> {
  return browser.newContext({
    viewport: { width: 1376, height: 985 },
    reducedMotion: "reduce",
    recordVideo: { dir: artifacts },
  })
}

async function ready(page: Page) {
  await page.goto(origin)
  await page.getByRole("textbox", { name: "Message", exact: true }).waitFor()
}

async function submit(page: Page, message: string) {
  await page
    .getByRole("textbox", { name: "Message", exact: true })
    .fill(message)
  await page.getByRole("button", { name: "Send", exact: true }).click()
  await page
    .getByText("Your request is saved and queued.", { exact: false })
    .waitFor()
}

async function openSaved(page: Page, title: string) {
  await ready(page)
  await page.getByRole("button", { name: title, exact: true }).click()
}

function commands(page: Page) {
  const methods: string[] = []
  page.on("websocket", (socket) =>
    socket.on("framesent", ({ payload }) => {
      try {
        const message = JSON.parse(String(payload))
        if (typeof message.method === "string") methods.push(message.method)
      } catch {
        // Only JSON-RPC frames are relevant to the assertion.
      }
    })
  )
  return methods
}

beforeAll(async () => {
  if (
    !environment.TRELLIS_TEST_DATABASE_URL ||
    !environment.TRELLIS_WORKER_DATABASE_URL
  )
    throw new Error(
      "Set the disposable admin and restricted worker database URLs before running browser tests"
    )
  await mkdir(artifacts, { recursive: true })
  const seed = start([python, "-m", "tests.durable_harness", "seed"])
  expect(await seed.exited).toBe(0)
  await startApi()
  vite = start(
    [
      process.env.TRELLIS_TEST_NODE ?? "node",
      path.resolve(frontend, "node_modules/vite/bin/vite.js"),
      "--config",
      "tests/browser/vite.config.mts",
    ],
    frontend
  )
  await until(async () => {
    try {
      return (await fetch(origin)).ok
    } catch {
      return false
    }
  })
  browser = await chromium.launch({ headless: true })
})

afterAll(async () => {
  await browser?.close()
  await Promise.all(workers.map(stop))
  await stop(api)
  await stop(vite)
})

afterEach(async () => {
  for (const user of browser?.contexts() ?? []) {
    for (const page of user.pages()) {
      await page
        .screenshot({
          path: path.join(artifacts, `remaining-${crypto.randomUUID()}.png`),
        })
        .catch(() => undefined)
    }
    await user.close()
  }
  await Promise.all(workers.map(stop))
  workers = []
})

test("accepted work survives API restart and a closed website, then replays and follows live output", async () => {
  const user = await context()
  let page = await user.newPage()
  const message = `Continue after closing ${crypto.randomUUID()}`
  await ready(page)
  await submit(page, message)
  await page.screenshot({ path: path.join(artifacts, "queued.png") })
  await page.close()
  await page.video()?.saveAs(path.join(artifacts, "01-queued.webm"))
  await stop(api)
  await startApi()
  workers = [
    start([python, "-m", "tests.durable_harness", "worker"]),
    start([python, "-m", "tests.durable_harness", "worker"]),
  ]
  page = await user.newPage()
  const sent = commands(page)
  await openSaved(page, message)
  await page
    .locator(".thread-message.assistant")
    .getByText("Durable worker", { exact: false })
    .waitFor()
  await page.screenshot({ path: path.join(artifacts, "following.png") })
  await page.close()
  await page.video()?.saveAs(path.join(artifacts, "02-live-reconnect.webm"))
  page = await user.newPage()
  const reopened = commands(page)
  await openSaved(page, message)
  await page.getByText("Durable worker answer.", { exact: true }).waitFor()
  await page.screenshot({ path: path.join(artifacts, "completed.png") })
  expect(
    await page
      .locator(".thread-message.user")
      .filter({ hasText: message })
      .count()
  ).toBe(1)
  expect(
    await page
      .locator(".thread-message.assistant")
      .filter({ hasText: "Durable worker answer." })
      .count()
  ).toBe(1)
  expect(sent).toContain("run.resume")
  expect(sent).not.toContain("run.start")
  expect(reopened).not.toContain("run.start")
  await user.close()
  await page
    .video()
    ?.saveAs(path.join(artifacts, "03-completed-reconnect.webm"))
  await Promise.all(workers.map(stop))
  workers = []
})

test("a lost acceptance response is looked up without a second submission", async () => {
  const user = await context()
  let dropped = false
  await user.routeWebSocket("**/api/runtime", (socket) => {
    const server = socket.connectToServer()
    let startId: string | null = null
    socket.onMessage((data) => {
      const request = JSON.parse(String(data))
      if (request.method === "run.start") startId = request.id
      server.send(data)
    })
    server.onMessage((data) => {
      const response = JSON.parse(String(data))
      if (
        !dropped &&
        startId &&
        response.id === startId &&
        response.result?.runId
      ) {
        dropped = true
        socket.close({
          code: 1012,
          reason: "Deliberate lost acceptance response",
        })
        return
      }
      socket.send(data)
    })
  })
  const page = await user.newPage()
  const sent = commands(page)
  await ready(page)
  await submit(page, `Lost acceptance ${crypto.randomUUID()}`)
  expect(dropped).toBe(true)
  expect(sent.filter((method) => method === "run.start")).toHaveLength(1)
  expect(sent).toContain("run.lookup")
  expect(sent).toContain("run.resume")
  await page
    .getByRole("button", { name: "Stop generating", exact: true })
    .click()
  await page.getByRole("button", { name: "Cancelled", exact: false }).waitFor()
  await user.close()
})

test("sign-out clears browser metadata while the worker finishes accepted work", async () => {
  workers = [start([python, "-m", "tests.durable_harness", "worker"])]
  const user = await context()
  const page = await user.newPage()
  const message = `Continue after sign-out ${crypto.randomUUID()}`
  await ready(page)
  await submit(page, message)
  await page
    .getByRole("button", { name: /Thinking|Answering/, exact: false })
    .waitFor()
  await page.locator("summary[aria-label^='Account:']").click()
  await page.getByRole("button", { name: "Sign out", exact: true }).click()
  await page.getByRole("button", { name: /Google/ }).waitFor()
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) =>
        key.startsWith("trellis.pending-run.")
      )
    )
  ).toEqual([])
  await page.getByRole("button", { name: /Google/ }).click()
  const sent = commands(page)
  await openSaved(page, message)
  await page.getByText("Durable worker answer.", { exact: true }).waitFor()
  expect(sent).not.toContain("run.start")
  await page.screenshot({ path: path.join(artifacts, "signed-in-result.png") })
  await user.close()
  await Promise.all(workers.map(stop))
  workers = []
})

test("an interrupted run preserves partial output and requires an explicit retry", async () => {
  workers = [start([python, "-m", "tests.durable_harness", "worker"])]
  const user = await context()
  const page = await user.newPage()
  const sent = commands(page)
  const message = `Interrupted answer ${crypto.randomUUID()}`
  await ready(page)
  await submit(page, message)
  await page
    .locator(".thread-message.assistant")
    .getByText("Durable worker", { exact: false })
    .waitFor()
  workers[0]!.kill("SIGKILL")
  await workers[0]!.exited
  workers = []
  const sessions = (await (
    await fetch("http://127.0.0.1:8123/api/sessions", {
      headers: { Authorization: "Bearer alice" },
    })
  ).json()) as { id: string; title: string }[]
  const saved = sessions.find((session) => session.title === message)!
  const latest = (await (
    await fetch(`http://127.0.0.1:8123/api/sessions/${saved.id}/runs/latest`, {
      headers: { Authorization: "Bearer alice" },
    })
  ).json()) as { run_id: string }
  const expiry = start([
    python,
    "-m",
    "tests.durable_harness",
    "expire",
    latest.run_id,
  ])
  expect(await expiry.exited).toBe(0)
  workers = [start([python, "-m", "tests.durable_harness", "worker"])]
  await page
    .locator(".run-activity-trigger")
    .filter({ hasText: /^Interrupted/ })
    .waitFor()
  await page.getByRole("button", { name: "Retry", exact: true }).waitFor()
  expect(
    await page
      .locator(".thread-message.assistant")
      .getByText("Durable worker", { exact: true })
      .count()
  ).toBe(1)
  expect(sent.filter((method) => method === "run.start")).toHaveLength(1)
  await page.screenshot({ path: path.join(artifacts, "interrupted.png") })
  await page.getByRole("button", { name: "Retry", exact: true }).click()
  await page.getByText("Durable worker answer.", { exact: true }).waitFor()
  expect(sent.filter((method) => method === "run.start")).toHaveLength(2)
  await user.close()
})

test("completion during discovery refreshes history before finishing restoration", async () => {
  const user = await context()
  const page = await user.newPage()
  const message = `Complete during discovery ${crypto.randomUUID()}`
  await ready(page)
  await submit(page, message)
  let heldHistory = false
  await page.route("**/api/sessions/*/history?*", async (route) => {
    if (heldHistory) return route.continue()
    heldHistory = true
    const response = await route.fetch()
    const previous = await response.json()
    const sessionId = new URL(route.request().url()).pathname.split("/")[3]
    expect(previous.assistant_outputs ?? []).toHaveLength(0)
    workers = [start([python, "-m", "tests.durable_harness", "worker"])]
    await until(async () => {
      const current = (await (
        await fetch(
          `http://127.0.0.1:8123/api/sessions/${sessionId}/runs/latest`,
          {
            headers: { Authorization: "Bearer alice" },
          }
        )
      ).json()) as { status: string }
      return current.status === "completed"
    })
    await route.fulfill({ response, json: previous })
  })
  await openSaved(page, message)
  await page
    .getByText("Durable worker answer.", { exact: true })
    .waitFor({ timeout: 10_000 })
  expect(heldHistory).toBe(true)
  await user.close()
})

test("another signed-in user cannot open the saved run", async () => {
  const alice = await context()
  const page = await alice.newPage()
  await ready(page)
  const message = `Owned request ${crypto.randomUUID()}`
  await submit(page, message)
  const sessions = (await (
    await fetch("http://127.0.0.1:8123/api/sessions", {
      headers: { Authorization: "Bearer alice" },
    })
  ).json()) as { id: string; title: string }[]
  const saved = sessions.find((session) => session.title === message)
  expect(saved).toBeDefined()
  const denied = await fetch(
    `http://127.0.0.1:8123/api/sessions/${saved!.id}`,
    { headers: { Authorization: "Bearer bob" } }
  )
  expect(denied.status).toBe(404)
  await page
    .getByRole("button", { name: "Stop generating", exact: true })
    .click()
  await alice.close()
})
