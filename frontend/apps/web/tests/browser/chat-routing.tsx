import * as Sentry from "@sentry/react"
import { createRoot } from "react-dom/client"
import type { User } from "@supabase/supabase-js"

import "@trellis/ui/globals.css"
import { AppShell } from "../../src/components/app-shell"
import { AuthGate } from "../../src/components/auth-gate"
import { ThemeProvider } from "../../src/components/theme-provider"
import { ApiError, api } from "../../src/lib/api"
import {
  getAuthController,
  type AuthState,
} from "../../src/lib/auth-controller"
import { chatPath, navigate } from "../../src/lib/app-routes"
import type {
  LatestRun,
  Message,
  Session,
  SessionHistoryPage,
} from "../../src/lib/app-types"
import { createTelemetryOptions } from "../../src/lib/telemetry"
import { parseTelemetryConfig } from "../../src/lib/telemetry-policy"

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
const latest = new Map<string, LatestRun>()
const requests: string[] = []
const rpc: string[] = []
const checks: string[] = []
type Envelope = Parameters<
  ReturnType<NonNullable<Sentry.BrowserOptions["transport"]>>["send"]
>[0]
const envelopes: Envelope[] = []
const chatPropagation: ReturnType<typeof Sentry.getTraceData>[] = []
function exportedSpanNames() {
  return envelopes.flatMap((envelope) =>
    envelope[1].flatMap(([header, payload]) =>
      header.type === "span"
        ? (payload as { items: { name: string }[] }).items.map(
            (span) => span.name
          )
        : []
    )
  )
}
const listeners = new Set<() => void>()
const sockets: FixtureSocket[] = []
let transientFailed = true
let releaseHistory: (() => void) | null = null
let delayMissing = false
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
  getAccessToken: async () => credential,
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
    timeline_order: [],
    next_before_cursor: null,
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
  listRunSummaries: async () => ({ items: [], next_offset: null }),
  getLatestRun: async (sessionId: string) => latest.get(sessionId) ?? null,
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
      }
      this.runId = `run-${input.sessionId}`
      const inputMessage: Message = {
        id: "fixture-input",
        turn_id: input.turnId,
        role: "user",
        content: input.content,
        provider: null,
        model: null,
        created_at: now,
      }
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
      result = { runId: this.runId, modelId: "fixture-model" }
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

const telemetry = Sentry.init({
  ...createTelemetryOptions(
    parseTelemetryConfig({
      VITE_SENTRY_ENABLED: "true",
      VITE_SENTRY_DSN: "https://public@o123.ingest.sentry.io/456",
      VITE_SENTRY_TRACES_SAMPLE_RATE: "1",
      origin: window.location.origin,
    })
  ),
  transport: () => ({
    send(envelope) {
      envelopes.push(envelope)
      return Promise.resolve({ statusCode: 200 })
    },
    flush: () => Promise.resolve(true),
  }),
})!
function evidence() {
  document.getElementById("fixture-evidence")!.textContent = JSON.stringify(
    {
      path: window.location.pathname,
      checks,
      requests,
      rpc,
      historyLength: window.history.length,
      privacy: Object.values(ids).some((id) =>
        JSON.stringify({
          envelopes,
          chatPropagation,
          baggage: Sentry.getTraceData(),
        }).includes(id)
      )
        ? "FAIL"
        : "PASS",
      envelopes: envelopes.length,
      exportedSpanNames: exportedSpanNames(),
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
async function submit(content: string) {
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
    () => !!document.querySelector(".cancel-run-button"),
    "Run did not start"
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
  Sentry.captureException(new Error("synthetic-chat-error"))
  chatPropagation.push(Sentry.getTraceData())
  await until(
    () => exportedSpanNames().includes("/chats/:chatId"),
    "No normalized chat span was exported"
  )
  await telemetry.flush(1000)
  assert(
    !Object.values(ids).some((id) =>
      JSON.stringify({ envelopes, chatPropagation }).includes(id)
    ),
    "Chat UUID escaped while its route was active"
  )
  pass(
    "Active chat exports a /chats/:chatId span and keeps UUIDs out of error envelopes and baggage"
  )
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
  Sentry.captureException(new Error("synthetic-error"))
  await telemetry.flush(1000)
  const encoded = JSON.stringify({
    envelopes,
    chatPropagation,
    baggage: Sentry.getTraceData(),
  })
  assert(
    !Object.values(ids).some((id) => encoded.includes(id)),
    "Chat ID escaped into telemetry"
  )
  pass("Exported Sentry envelopes and propagation data contain no chat UUID")
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
