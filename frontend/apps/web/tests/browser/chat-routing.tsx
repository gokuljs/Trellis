import { createRoot } from "react-dom/client"
import type { User } from "@supabase/supabase-js"

import "@trellis/ui/globals.css"
import { AppShell } from "../../src/components/app-shell"
import { AuthGate } from "../../src/components/auth-gate"
import { ThemeProvider } from "../../src/components/theme-provider"
import { ApiError, api } from "../../src/lib/api"
import {
  AuthAccessError,
  getAuthController,
  type AuthState,
} from "../../src/lib/auth-controller"
import { readPendingRun } from "../../src/lib/pending-run"
import { chatPath, navigate } from "../../src/lib/app-routes"
import type {
  AssistantOutput,
  LatestRun,
  Message,
  RunSummary,
  Session,
  SessionHistoryPage,
} from "../../src/lib/app-types"

const ids = {
  a: "11111111-1111-4111-a111-111111111111",
  b: "22222222-2222-4222-a222-222222222222",
  offPage: "33333333-3333-4333-a333-333333333333",
  missing: "44444444-4444-4444-a444-444444444444",
  transient: "55555555-5555-4555-a555-555555555555",
  created: "66666666-6666-4666-a666-666666666666",
}
const now = "2026-10-18T00:00:00Z"
const sessions = new Map(
  Object.entries(ids).map(([name, id]) => [
    id,
    {
      id,
      title: `Fixture chat ${name}`,
      created_at: now,
      updated_at: now,
      message_count: 2,
      model_id: "fixture-model",
    } satisfies Session,
  ])
)
const transcripts = new Map<string, Message[]>()
const savedOutputs = new Map<string, AssistantOutput[]>()
const latest = new Map<string, LatestRun>()
const requests: string[] = []
const rpc: string[] = []
const startRequests: { clientRequestId: string; sessionId: string }[] = []
const acceptedRequests = new Map<string, string>()
const checks: string[] = []
const browserLogs: unknown[] = []
const blockedRequests: string[] = []
const originalConsoleInfo = console.info.bind(console)
const originalConsoleWarn = console.warn.bind(console)
const originalConsoleError = console.error.bind(console)
console.info = (...args) => {
  browserLogs.push(args)
  originalConsoleInfo(...args)
}
console.warn = (...args) => {
  browserLogs.push(args)
  originalConsoleWarn(...args)
}
console.error = (...args) => {
  browserLogs.push(args)
  originalConsoleError(...args)
}
const listeners = new Set<() => void>()
const sockets: FixtureSocket[] = []
let transientFailed = true
let releaseHistory: (() => void) | null = null
let delayMissing = false
let delayDiscovery = false
let failDiscovery = false
let releaseDiscovery: (() => void) | null = null
let loseAcceptanceReply = false
let accessTokenUnavailable = false
let acceptanceFailure: string | null = null
let busyFailure: string | null = null
let rejectBeforeAcceptance = false
const user = { id: "fixture-user", email: "fixture@example.test" } as User
const credential = { token: "fixture-token", userId: user.id, sessionEpoch: 1 }
let authState: AuthState = {
  status: "signed-in",
  user,
  error: null,
  pendingProvider: null,
  sessionEpoch: 1,
}

function publishAuth(
  status: "signed-in" | "signed-out",
  error: string | null = null
) {
  authState = {
    ...authState,
    status,
    user: status === "signed-in" ? user : null,
    error,
  }
  for (const listener of listeners) listener()
}
Object.assign(getAuthController(), {
  getSnapshot: () => authState,
  getSessionEpoch: () => 1,
  getAccessToken: async () => {
    if (accessTokenUnavailable)
      throw new AuthAccessError("auth_unavailable", "Synthetic auth outage")
    return credential
  },
  isCredentialCurrent: () => true,
  expireSession: async () => publishAuth("signed-out"),
  subscribe(listener: () => void) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  signIn: async () =>
    publishAuth("signed-out", "Synthetic failed sign-in. Try again."),
  signOut: async () => publishAuth("signed-out"),
  retry: async () => undefined,
})
window.fetch = async (input) => {
  const path = new URL(String(input), window.location.href).pathname
  if (path === "/health" || path === "/ready")
    return Response.json({ status: path === "/ready" ? "ready" : "ok" })
  blockedRequests.push(path)
  throw new Error(`Fixture blocked unexpected network request ${path}`)
}

function savedMessages(sessionId: string): Message[] {
  return (
    transcripts.get(sessionId) ?? [
      {
        id: `saved-${sessionId}`,
        turn_id: `turn-${sessionId}`,
        role: "assistant",
        content: `Saved reply for ${sessions.get(sessionId)!.title}`,
        provider: "fixture",
        model: "fixture-model",
        created_at: now,
      },
    ]
  )
}
function history(sessionId: string): SessionHistoryPage {
  return {
    session: sessions.get(sessionId)!,
    messages: savedMessages(sessionId),
    assistant_outputs: savedOutputs.get(sessionId) ?? [],
    timeline_order: [],
    next_before_cursor: null,
  }
}
function runSummary(run: LatestRun): RunSummary {
  return {
    ...run,
    retry_of: null,
    budget_preset: "conservative",
    max_model_calls: 8,
    max_tool_calls: 16,
    max_total_tokens: 100000,
    max_cost_usd: 2,
    deadline_at: "2026-10-18T00:10:00Z",
    created_at: now,
    started_at: now,
    finished_at: ["completed", "cancelled", "failed", "interrupted"].includes(
      run.status
    )
      ? "2026-10-18T00:00:10Z"
      : null,
    provider_id: "fixture",
    model_id: "fixture-model",
    model_call_count: 1,
    tool_call_count: 0,
    total_tokens: null,
    usage_complete: false,
    estimated_cost_usd: null,
    cost_complete: false,
    stop_reason: null,
    error_code: null,
    error_message: null,
  }
}
Object.assign(api, {
  getProfile: async () => ({
    id: user.id,
    email: user.email,
    display_name: "Fixture",
    created_at: now,
    updated_at: now,
  }),
  getSettings: async () => ({
    selected_provider: "fixture",
    selected_model_id: "fixture-model",
    providers: [],
    models: [
      {
        id: "fixture-model",
        provider_id: "fixture",
        provider_name: "Fixture",
        adapter_kind: "fixture",
        upstream_model_id: "fixture-model",
        name: "Fixture model",
        requires_api_key: false,
        supports_streaming: true,
        supports_tools: true,
        configured: true,
        key_hint: null,
      },
    ],
  }),
  getOnboarding: async () => ({ completed: true, current_step: "complete" }),
  listSessionPages: async () => ({
    items: [sessions.get(ids.a), sessions.get(ids.b)],
    next_before_cursor: null,
  }),
  getSessionHistory: async (sessionId: string) => {
    requests.push(`history:${sessionId}`)
    if (sessionId === ids.missing) {
      if (delayMissing)
        await new Promise<void>((resolve) => {
          releaseHistory = resolve
        })
      throw new ApiError(
        "session_not_found",
        "Private server detail must stay hidden",
        404
      )
    }
    if (sessionId === ids.transient && transientFailed)
      throw new ApiError(
        "service_unavailable",
        "Synthetic temporary outage",
        503
      )
    return history(sessionId)
  },
  listRunSummaries: async (sessionId: string) => {
    const run = latest.get(sessionId)
    return { items: run ? [runSummary(run)] : [], next_offset: null }
  },
  getWorkspaceReview: async () => ({ blocked: false }),
  getLatestRun: async (sessionId: string) => {
    if (sessionId === ids.a && delayDiscovery)
      await new Promise<void>((resolve) => {
        releaseDiscovery = resolve
      })
    if (sessionId === ids.a && failDiscovery)
      throw new ApiError(
        "service_unavailable",
        "Synthetic discovery outage",
        503
      )
    return latest.get(sessionId) ?? null
  },
  createSession: async () => {
    requests.push("create")
    transcripts.set(ids.created, [])
    return sessions.get(ids.created)
  },
  selectSessionModel: async (sessionId: string) => sessions.get(sessionId),
  previewContext: async () => ({
    model_id: "fixture-model",
    context_window_tokens: null,
    input_tokens: null,
    count_method: "utf8_bytes_estimate_v1",
    output_reserve_tokens: null,
    safety_margin_tokens: null,
    usable_input_tokens: null,
    compaction_threshold_tokens: null,
    percentage: null,
    status: "unavailable",
  }),
})

class FixtureSocket {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false
  runId: string | null = null
  constructor() {
    sockets.push(this)
    queueMicrotask(() => this.onopen?.())
  }
  send(raw: string) {
    const frame = JSON.parse(raw)
    rpc.push(frame.method)
    let result: unknown = { userId: user.id }
    if (frame.method === "run.start") {
      const input = frame.params as {
        sessionId: string
        turnId: string
        content: string
        clientRequestId: string
      }
      startRequests.push({
        clientRequestId: input.clientRequestId,
        sessionId: input.sessionId,
      })
      if (rejectBeforeAcceptance) {
        rejectBeforeAcceptance = false
        this.reject(frame.id, "database_unavailable")
        return
      }
      if (busyFailure) {
        const code = busyFailure
        busyFailure = null
        const otherInput: Message = {
          id: "other-tab-input",
          turn_id: `other-${input.turnId}`,
          role: "user",
          content: "Work accepted in another tab",
          provider: null,
          model: null,
          created_at: now,
        }
        transcripts.set(input.sessionId, [
          ...savedMessages(input.sessionId),
          otherInput,
        ])
        latest.set(input.sessionId, {
          run_id: `other-${input.clientRequestId}`,
          turn_id: otherInput.turn_id,
          status: "running",
          last_sequence: 0,
          input_message: otherInput,
        })
        this.reject(frame.id, code)
        return
      }
      this.runId =
        acceptedRequests.get(input.clientRequestId) ?? `run-${input.sessionId}`
      const existing = acceptedRequests.has(input.clientRequestId)
      acceptedRequests.set(input.clientRequestId, this.runId)
      const inputMessage: Message = {
        id: "fixture-input",
        turn_id: input.turnId,
        role: "user",
        content: input.content,
        provider: null,
        model: null,
        created_at: now,
      }
      if (!existing)
        transcripts.set(input.sessionId, [
          ...savedMessages(input.sessionId),
          inputMessage,
        ])
      latest.set(input.sessionId, {
        run_id: this.runId,
        turn_id: input.turnId,
        status: "running",
        last_sequence: 0,
        input_message: inputMessage,
      })
      result = {
        runId: this.runId,
        status: "running",
        modelId: "fixture-model",
      }
      if (acceptanceFailure) {
        const code = acceptanceFailure
        acceptanceFailure = null
        this.reject(frame.id, code)
        return
      }
      if (loseAcceptanceReply) {
        loseAcceptanceReply = false
        queueMicrotask(() => {
          this.closed = true
          this.onclose?.({ code: 1006 })
        })
        return
      }
    } else if (frame.method === "run.lookup") {
      const runId = acceptedRequests.get(frame.params.clientRequestId)
      const saved = latest.get(frame.params.sessionId)
      result =
        runId && saved?.run_id === runId
          ? { runId, status: saved.status }
          : null
    } else if (frame.method === "run.cancel") {
      const saved = [...latest.values()].find(
        (run) => run.run_id === frame.params.runId
      )!
      saved.status = "cancelled"
      result = { runId: saved.run_id, status: "cancelled" }
      for (const socket of sockets) {
        if (!socket.closed && socket.runId === saved.run_id)
          socket.event("run.cancelled", {})
      }
    } else if (frame.method === "run.resume") {
      this.runId = frame.params.runId
      result = { runId: this.runId, modelId: "fixture-model" }
    }
    queueMicrotask(() =>
      this.onmessage?.({
        data: JSON.stringify({ jsonrpc: "2.0", id: frame.id, result }),
      })
    )
  }
  close() {
    this.closed = true
    this.onclose?.({ code: 1000 })
  }
  reject(id: string, code: string) {
    queueMicrotask(() =>
      this.onmessage?.({
        data: JSON.stringify({
          jsonrpc: "2.0",
          id,
          error: {
            code: -32000,
            message: "Synthetic request failure",
            data: { code, status: 503 },
          },
        }),
      })
    )
  }
  event(eventType: string, data: Record<string, unknown>, sequence = 1) {
    this.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        method: "run.event",
        params: {
          runId: this.runId,
          sequence,
          eventType,
          eventVersion: 1,
          data,
        },
      }),
    })
  }
  staleEvent() {
    this.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        method: "run.event",
        params: {
          runId: this.runId,
          sequence: 1,
          eventType: "assistant.delta",
          eventVersion: 1,
          data: { text: "STALE PRIVATE A EVENT" },
        },
      }),
    })
  }
}
window.WebSocket = FixtureSocket as unknown as typeof WebSocket

function evidence() {
  document.getElementById("fixture-evidence")!.textContent = JSON.stringify(
    {
      path: window.location.pathname,
      checks,
      requests,
      rpc,
      startRequests,
      historyLength: window.history.length,
      privacy: Object.values(ids).some((id) =>
        JSON.stringify(browserLogs).includes(id)
      )
        ? "FAIL"
        : "PASS",
      blockedRequests,
    },
    null,
    2
  )
}
window.addEventListener("popstate", evidence)
createRoot(document.getElementById("root")!).render(
  <ThemeProvider>
    <AuthGate>
      <AppShell />
    </AuthGate>
  </ThemeProvider>
)

async function until(check: () => boolean, message: string) {
  const deadline = Date.now() + 8000
  while (!check()) {
    if (Date.now() > deadline) throw new Error(message)
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
}
function assert(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message)
}
function button(label: string) {
  const found = [...document.querySelectorAll("#root button")].find(
    (item) =>
      item.getAttribute("aria-label") === label ||
      item.textContent?.trim() === label ||
      item.querySelector("span:not(.shortcut-hint)")?.textContent?.trim() ===
        label
  )
  assert(found, `Missing button ${label}`)
  return found as HTMLButtonElement
}
async function open(sessionId: string) {
  navigate(chatPath(sessionId))
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
        sessions.get(sessionId)!.title &&
      !document.querySelector<HTMLTextAreaElement>("textarea")?.disabled,
    `Chat ${sessionId} did not load`
  )
}
function pass(message: string) {
  checks.push(`PASS ${message}`)
  evidence()
}
async function submit(content: string, accepted = true) {
  const textarea = document.querySelector("textarea")!
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value"
  )!.set!
  setter.call(textarea, content)
  textarea.dispatchEvent(new Event("input", { bubbles: true }))
  await new Promise((resolve) => setTimeout(resolve, 30))
  textarea.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Enter", bubbles: true })
  )
  await until(
    () =>
      accepted
        ? !!document.querySelector(".cancel-run-button")
        : document
            .querySelector(".chat-error")
            ?.textContent?.includes("Your request was not accepted.") ===
            true &&
          !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Submission did not reach its expected state"
  )
}
async function runChecks() {
  await until(
    () => !!document.querySelector(".app-shell"),
    "Workspace did not bootstrap"
  )
  await open(ids.a)
  await open(ids.offPage)
  assert(
    requests.includes(`history:${ids.offPage}`),
    "Off-page chat was not requested directly"
  )
  pass("Deep link loads a chat outside the sidebar page")
  assert(
    !Object.values(ids).some((id) => JSON.stringify(browserLogs).includes(id)),
    "Chat UUID escaped into browser logs"
  )
  pass("Browser logs exclude chat UUIDs while a chat route is active")
  await open(ids.a)
  const count = window.history.length
  button("Fixture chat a").click()
  assert(window.history.length === count, "Same chat duplicated history")
  button("Settings").click()
  await until(
    () => !!document.querySelector(".settings-content"),
    "Settings did not open"
  )
  button("Fixture chat a").click()
  await until(
    () => !!document.querySelector(".thread-content"),
    "Same chat did not reopen from Settings"
  )
  assert(window.history.length === count, "Settings created a route")
  pass(
    "Same selection reopens chat from local Settings without duplicate history"
  )
  await open(ids.b)
  window.history.back()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
      "Fixture chat a",
    "Back did not restore A"
  )
  window.history.forward()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
      "Fixture chat b",
    "Forward did not restore B"
  )
  pass("Back and Forward restore chat selection")
  navigate(chatPath(ids.missing))
  await until(
    () =>
      window.location.pathname === "/" &&
      !!document.querySelector(".welcome-error"),
    "Missing chat did not return home"
  )
  assert(
    document.querySelector(".welcome-error")!.textContent ===
      "This chat is unavailable.",
    "Missing chat revealed server details"
  )
  pass("Unavailable chat replaces its route and shows a generic message")
  navigate(chatPath(ids.transient))
  await until(
    () => !!document.querySelector(".welcome-error button"),
    "Transient failure did not offer Retry"
  )
  assert(
    window.location.pathname === chatPath(ids.transient),
    "Transient failure lost route"
  )
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Transient failure allowed sending"
  )
  transientFailed = false
  button("Retry").click()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
      "Fixture chat transient",
    "Retry did not load chat"
  )
  pass(
    "Transient failure keeps its route, disables sending, and recovers with Retry"
  )
  delayMissing = true
  navigate(chatPath(ids.missing))
  await until(
    () => releaseHistory !== null,
    "Delayed history request did not start"
  )
  await open(ids.b)
  releaseHistory!()
  await new Promise((resolve) => setTimeout(resolve, 80))
  assert(
    window.location.pathname === chatPath(ids.b),
    "Stale 404 replaced the current route"
  )
  pass("A stale unavailable response cannot replace a newer chat")
  await open(ids.a)
  await submit("Keep running while I switch")
  const firstSocket = sockets.at(-1)!
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Running chat allowed input"
  )
  assert(button("Send").disabled, "Running chat allowed Send")
  assert(
    document.querySelector<HTMLSelectElement>('[aria-label="Run budget"]')!
      .disabled,
    "Running chat allowed budget changes"
  )
  assert(
    document.querySelector<HTMLSelectElement>('[aria-label="Chat model"]')!
      .disabled,
    "Running chat allowed model changes"
  )
  assert(!button("Stop generating").disabled, "Composer lock blocked Stop")
  pass(
    "A running chat locks message, Send, model, and budget while Stop remains usable"
  )
  button("Fixture chat b").click()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
        "Fixture chat b" &&
      !document.querySelector<HTMLTextAreaElement>("textarea")?.disabled,
    "Pending sidebar switch did not load B"
  )
  firstSocket.staleEvent()
  await new Promise((resolve) => setTimeout(resolve, 50))
  assert(
    !document
      .querySelector("#root")!
      .textContent!.includes("STALE PRIVATE A EVENT"),
    "A event leaked into B"
  )
  assert(!rpc.includes("run.cancel"), "Switch cancelled the original run")
  assert(firstSocket.closed, "Switch did not close the browser subscription")
  window.history.back()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
        "Fixture chat a" && rpc.includes("run.resume"),
    "Returning to A did not resume"
  )
  window.history.forward()
  await until(
    () =>
      document.querySelector(".topbar-current")?.textContent ===
        "Fixture chat b" &&
      !document.querySelector<HTMLTextAreaElement>("textarea")?.disabled,
    "Pending Forward did not clear A state"
  )
  assert(
    !rpc.includes("run.cancel"),
    "History navigation cancelled the original run"
  )
  pass(
    "Pending sidebar and history switches preserve the run, ignore stale events, and resume on return"
  )
  delayDiscovery = true
  navigate(chatPath(ids.a))
  await until(() => releaseDiscovery !== null, "Run discovery did not pause")
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Delayed discovery briefly enabled input"
  )
  assert(
    document.querySelector(".composer-footnote")?.textContent ===
      "Checking current run…",
    "Discovery status is unclear"
  )
  delayDiscovery = false
  releaseDiscovery!()
  await until(
    () => !!document.querySelector(".cancel-run-button"),
    "Delayed discovery did not restore the run"
  )
  assert(
    rpc.includes("run.lookup"),
    "Returning did not look up the exact request"
  )
  assert(
    rpc.filter((method) => method === "run.start").length === 1,
    "Returning submitted again"
  )
  pass(
    "Exact request lookup and delayed discovery keep the chat locked without another submission"
  )
  await open(ids.b)
  failDiscovery = true
  navigate(chatPath(ids.a))
  await until(
    () =>
      !![...document.querySelectorAll("button")].find(
        (item) => item.textContent?.trim() === "Reconnect"
      ),
    "Discovery failure did not offer Reconnect"
  )
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Discovery failure unlocked input"
  )
  failDiscovery = false
  button("Reconnect").click()
  await until(
    () =>
      !!document.querySelector(".cancel-run-button") &&
      !document.querySelector(".chat-error"),
    "Reconnect did not restore the run"
  )
  button("Stop generating").click()
  await until(
    () => !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Cancellation did not unlock input"
  )
  assert(
    rpc.filter((method) => method === "run.start").length === 1,
    "Reconnect or Stop submitted again"
  )
  pass(
    "Discovery failure keeps the lock and Stop works after reconnect until terminal cancellation"
  )
  await open(ids.b)
  const finished = latest.get(ids.a)!
  finished.status = "completed"
  transcripts.set(ids.a, [
    ...savedMessages(ids.a),
    {
      id: "saved-completion",
      turn_id: finished.turn_id,
      role: "assistant",
      content: "Saved final answer",
      provider: "fixture",
      model: "fixture-model",
      created_at: now,
    },
  ])
  const resumesBeforeCompletion = rpc.filter(
    (method) => method === "run.resume"
  ).length
  await open(ids.a)
  assert(
    document
      .querySelector("#root")!
      .textContent!.includes("Saved final answer"),
    "Completed answer was not restored"
  )
  assert(
    rpc.filter((method) => method === "run.resume").length ===
      resumesBeforeCompletion,
    "Completed history opened another live run"
  )
  pass(
    "Returning after completion shows the saved answer and unlocks controls without execution"
  )
  await open(ids.b)
  const racingInput: Message = {
    id: "racing-input",
    turn_id: "racing-turn",
    role: "user",
    content: "Complete while the browser discovers my run",
    provider: null,
    model: null,
    created_at: now,
  }
  transcripts.set(ids.a, [...savedMessages(ids.a), racingInput])
  latest.set(ids.a, {
    run_id: "racing-run",
    turn_id: racingInput.turn_id,
    status: "running",
    last_sequence: 0,
    input_message: racingInput,
  })
  delayDiscovery = true
  releaseDiscovery = null
  navigate(chatPath(ids.a))
  await until(() => releaseDiscovery !== null, "Racing discovery did not pause")
  latest.get(ids.a)!.status = "completed"
  transcripts.set(ids.a, [
    ...savedMessages(ids.a),
    {
      ...racingInput,
      id: "racing-answer",
      role: "assistant",
      content: "Final answer saved during run discovery",
      provider: "fixture",
      model: "fixture-model",
    },
  ])
  delayDiscovery = false
  releaseDiscovery!()
  await until(
    () =>
      document
        .querySelector("#root")!
        .textContent!.includes("Final answer saved during run discovery") &&
      !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Completion during discovery did not reload its saved answer"
  )
  pass(
    "Completion between history and run discovery reloads the saved final answer"
  )
  await open(ids.b)
  delayDiscovery = true
  releaseDiscovery = null
  navigate(chatPath(ids.a))
  await until(
    () => releaseDiscovery !== null,
    "Acceptance discovery did not pause"
  )
  const acceptedDuringDiscovery: Message = {
    ...racingInput,
    id: "late-accepted-input",
    turn_id: "late-accepted-turn",
    content: "Accepted input saved after the browser read history",
  }
  transcripts.set(ids.a, [...savedMessages(ids.a), acceptedDuringDiscovery])
  latest.set(ids.a, {
    run_id: "late-accepted-run",
    turn_id: acceptedDuringDiscovery.turn_id,
    status: "running",
    last_sequence: 0,
    input_message: acceptedDuringDiscovery,
  })
  delayDiscovery = false
  releaseDiscovery!()
  await until(
    () =>
      document
        .querySelector("#root")!
        .textContent!.includes(acceptedDuringDiscovery.content) &&
      !!document.querySelector(".cancel-run-button"),
    "Run discovery omitted the input accepted after history was read"
  )
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Late accepted input did not lock its chat"
  )
  pass(
    "Run discovery shows newly accepted input even when history was read earlier"
  )
  await until(
    () => sockets.at(-1)?.runId === "late-accepted-run",
    "Restored subscription did not start"
  )
  const interruptedSocket = sockets.at(-1)!
  interruptedSocket.event("run.phase", { phase: "answering" })
  await until(
    () =>
      [...document.querySelectorAll(".run-activity-trigger")].some((item) =>
        item.textContent?.includes("Answering")
      ),
    "Running activity did not show its model phase"
  )
  interruptedSocket.closed = true
  interruptedSocket.onclose?.({ code: 1013 })
  await until(
    () =>
      !![...document.querySelectorAll("button")].find(
        (item) => item.textContent?.trim() === "Reconnect"
      ),
    "Lost restored connection did not offer Reconnect"
  )
  latest.get(ids.a)!.status = "interrupted"
  latest.get(ids.a)!.last_sequence = 2
  savedOutputs.set(ids.a, [
    {
      id: "saved-partial",
      turn_id: acceptedDuringDiscovery.turn_id,
      run_id: "late-accepted-run",
      model_call_id: "interrupted-call",
      step_index: 1,
      content: "Saved partial answer before interruption",
      status: "partial",
      stop_reason: "runtime_shutdown",
      promoted_message_id: null,
      created_at: now,
    },
  ])
  const startsBeforeInterruption = startRequests.length
  button("Reconnect").click()
  await until(
    () =>
      !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled &&
      [...document.querySelectorAll(".run-activity-trigger")].some((item) =>
        item.textContent?.includes("Interrupted")
      ) &&
      document
        .querySelector("#root")!
        .textContent!.includes("Saved partial answer before interruption"),
    "Interruption discovery left running activity or lost saved partial output"
  )
  assert(
    startRequests.length === startsBeforeInterruption,
    "Interruption discovery restarted execution"
  )
  pass(
    "Interruption discovered after reconnect replaces the running phase, preserves partial output, and unlocks without execution"
  )
  button("New session").click()
  await until(
    () => window.location.pathname === "/",
    "New chat did not push root"
  )
  const draftHistoryLength = window.history.length
  await submit("New stable chat")
  assert(
    window.location.pathname === chatPath(ids.created),
    "Create did not install stable URL"
  )
  assert(
    window.history.length === draftHistoryLength,
    "Create pushed instead of replacing draft"
  )
  assert(
    rpc.filter((method) => method === "run.start").length === 2,
    "Created chat submit reloaded or started twice"
  )
  pass(
    "Creating a chat replaces the draft URL before its first run without resetting submission"
  )
  await open(ids.b)
  loseAcceptanceReply = true
  await submit("Save this request even when the reply is lost")
  const retried = startRequests.filter((request) => request.sessionId === ids.b)
  assert(
    retried.length === 2 &&
      new Set(retried.map((request) => request.clientRequestId)).size === 1,
    "Lost response changed the logical request ID"
  )
  assert(
    savedMessages(ids.b).filter((message) => message.role === "user").length ===
      1,
    "Lost response duplicated the saved input"
  )
  pass(
    "A lost acceptance reply retries the same request ID and saves one logical submission"
  )
  const accepted = readPendingRun(user.id, ids.b)
  assert(accepted?.runId, "Accepted request metadata is missing")
  const startsBeforeAuthFailure = startRequests.length
  accessTokenUnavailable = true
  const acceptedSocket = sockets.at(-1)!
  acceptedSocket.closed = true
  acceptedSocket.onclose?.({ code: 1013 })
  await until(
    () =>
      !![...document.querySelectorAll("button")].find(
        (item) => item.textContent?.trim() === "Reconnect"
      ),
    "Authentication outage did not offer Reconnect"
  )
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Authentication outage unlocked accepted work"
  )
  assert(
    readPendingRun(user.id, ids.b)?.runId === accepted.runId,
    "Authentication outage discarded the accepted request"
  )
  accessTokenUnavailable = false
  button("Reconnect").click()
  await until(
    () => !document.querySelector(".chat-error"),
    "Authentication recovery did not resume the run"
  )
  assert(
    startRequests.length === startsBeforeAuthFailure,
    "Authentication recovery resubmitted accepted work"
  )
  button("Stop generating").click()
  await until(
    () => !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Recovered run did not cancel"
  )
  pass(
    "Authentication loss preserves accepted work and its lock until reconnect and cancellation"
  )
  const startsBeforeDatabaseFailure = startRequests.length
  acceptanceFailure = "database_unavailable"
  await submit("Keep the committed request after an uncertain database reply")
  assert(
    document.querySelector<HTMLTextAreaElement>("textarea")!.disabled &&
      !!readPendingRun(user.id, ids.b)?.runId,
    "Uncertain acceptance did not restore and lock its saved run"
  )
  assert(
    startRequests.length === startsBeforeDatabaseFailure + 1,
    "Uncertain acceptance resubmitted instead of looking up the request"
  )
  button("Stop generating").click()
  await until(
    () => !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
    "Uncertain accepted run did not cancel"
  )
  pass(
    "An uncertain database acceptance is looked up and followed without another submission"
  )
  for (const code of ["run_in_progress", "session_run_busy"]) {
    const startsBeforeBusy = startRequests.length
    busyFailure = code
    await submit("Another tab may already be working")
    assert(
      document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
      "Another tab's active run did not lock this chat"
    )
    assert(
      startRequests.length === startsBeforeBusy + 1,
      "Busy chat recovery resubmitted"
    )
    button("Stop generating").click()
    await until(
      () => !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled,
      "Another tab's restored run did not cancel"
    )
  }
  pass(
    "Busy chat rejections discover and follow the work accepted in another tab"
  )
  const startsBeforeMissing = startRequests.length
  rejectBeforeAcceptance = true
  await submit(
    "Keep my unaccepted message available for an explicit retry",
    false
  )
  assert(
    !document.querySelector<HTMLTextAreaElement>("textarea")!.disabled &&
      !readPendingRun(user.id, ids.b),
    "Confirmed absence did not unlock the chat and clear its pending hint"
  )
  assert(
    startRequests.length === startsBeforeMissing + 1,
    "Confirmed absence automatically resubmitted the message"
  )
  pass(
    "Confirmed absence preserves an explicit retry instead of silently losing the input"
  )
  await open(ids.offPage)
  publishAuth("signed-out")
  await until(
    () => window.location.pathname === "/login",
    "Signed-out deep link did not reach login"
  )
  const signupLink = document.querySelector<HTMLAnchorElement>(
    '.auth-switch a[href="/signup"]'
  )
  assert(signupLink, "Missing Sign up link")
  signupLink.click()
  await until(
    () => window.location.pathname === "/signup",
    "Sign-up link did not route"
  )
  button("Continue with Google").click()
  await until(
    () => !!document.querySelector(".auth-error"),
    "Failed sign-in did not display"
  )
  navigate("/", true)
  await until(
    () => window.location.pathname === "/login",
    "Failed OAuth callback did not return to login"
  )
  navigate("/", true)
  publishAuth("signed-in")
  await until(
    () =>
      window.location.pathname === chatPath(ids.offPage) &&
      document.querySelector(".topbar-current")?.textContent ===
        "Fixture chat offPage",
    "Auth return did not restore deep link"
  )
  navigate("/")
  await until(
    () => !!document.querySelector(".welcome-state"),
    "Consumed auth return unexpectedly replayed"
  )
  assert(window.location.pathname === "/", "Consumed return path was replayed")
  pass(
    "Auth return survives login, signup, failed sign-in, and a root callback, then consumes once"
  )
  window.history.pushState(null, "", "/chats/invalid")
  window.dispatchEvent(new Event("popstate"))
  await until(
    () => window.location.pathname === "/",
    "Malformed route did not normalize"
  )
  window.history.pushState(null, "", "/unknown")
  window.dispatchEvent(new Event("popstate"))
  await until(
    () => window.location.pathname === "/",
    "Unknown route did not normalize"
  )
  pass("Malformed and unknown routes replace with root")
  assert(
    blockedRequests.length === 0,
    `Unexpected browser request: ${blockedRequests.join(", ")}`
  )
  assert(
    !Object.values(ids).some((id) => JSON.stringify(browserLogs).includes(id)),
    "Chat UUID escaped into browser logs"
  )
  pass(
    "Browser logs exclude chat UUIDs and the fixture sends no external requests"
  )
  checks.push("ALL ROUTING CHECKS PASSED")
  evidence()
}
document.getElementById("run-checks")!.addEventListener("click", () => {
  const control = document.getElementById("run-checks") as HTMLButtonElement
  control.disabled = true
  void runChecks().catch((error: unknown) => {
    checks.push(`FAIL ${String(error)}`)
    evidence()
  })
})
evidence()
