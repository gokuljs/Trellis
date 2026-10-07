import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { toast } from "sonner"
import { afterEach, describe, expect, it, vi } from "vitest"

import "@trellis/ui/globals.css"
import { App } from "@/App"
import { ThemeProvider } from "@/components/theme-provider"

const profile = {
  id: "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5",
  display_name: "Ada",
  email: "ada@example.com",
  created_at: "2026-08-24T10:00:00Z",
  updated_at: "2026-08-24T10:00:00Z",
}

const settings = {
  selected_provider: "openai",
  selected_model_id: "openai:gpt-5.5",
  default_budget_preset: "conservative",
  providers: [
    {
      id: "openai",
      name: "OpenAI",
      model: "gpt-5.5",
      configured: true,
      key_hint: "••••7890",
    },
    {
      id: "anthropic",
      name: "Anthropic",
      model: "claude-sonnet-5",
      configured: false,
      key_hint: null,
    },
  ],
  models: [
    {
      id: "openai:gpt-5.5",
      provider_id: "openai",
      provider_name: "OpenAI",
      adapter_kind: "openai",
      upstream_model_id: "gpt-5.5",
      name: "GPT-5.5",
      requires_api_key: true,
      supports_streaming: true,
      supports_tools: false,
      configured: true,
      key_hint: "••••7890",
    },
    {
      id: "anthropic:claude-sonnet-5",
      provider_id: "anthropic",
      provider_name: "Anthropic",
      adapter_kind: "anthropic",
      upstream_model_id: "claude-sonnet-5",
      name: "Claude Sonnet 5",
      requires_api_key: true,
      supports_streaming: true,
      supports_tools: false,
      configured: false,
      key_hint: null,
    },
  ],
}

let onboardingState = { current_step: "complete", completed: true }

type TestMessage = {
  id: string
  turn_id: string
  role: "user" | "assistant"
  content: string
  provider: string | null
  model: string | null
  created_at: string
}

const recentSession = {
  id: "session-recent",
  title: "Persisted conversation",
  created_at: "2026-08-24T10:00:00Z",
  updated_at: "2026-08-25T10:00:00Z",
  message_count: 2,
  workspace_path: null,
}

const olderSession = {
  id: "session-older",
  title: "Earlier notes",
  created_at: "2026-07-20T10:00:00Z",
  updated_at: "2026-07-20T10:00:00Z",
  message_count: 1,
  workspace_path: null,
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

function renderApp() {
  onboardingState = { current_step: "complete", completed: true }
  return render(
    <ThemeProvider defaultTheme="dark">
      <App />
    </ThemeProvider>
  )
}

function renderFirstRunApp() {
  onboardingState = { current_step: "intro", completed: false }
  return render(
    <ThemeProvider defaultTheme="dark">
      <App />
    </ThemeProvider>
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

class TestWebSocket {
  static instances: TestWebSocket[] = []
  static onSend: (
    socket: TestWebSocket,
    payload: Record<string, unknown>
  ) => void = () => {}
  static OPEN = 1
  static CONNECTING = 0
  readyState = TestWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  readonly url: string

  constructor(url: string) {
    this.url = url
    TestWebSocket.instances.push(this)
    queueMicrotask(() => {
      this.readyState = TestWebSocket.OPEN
      this.onopen?.()
    })
  }

  send(data: string) {
    TestWebSocket.onSend(this, JSON.parse(data) as Record<string, unknown>)
  }

  close() {
    this.readyState = 3
    this.onclose?.()
  }

  reply(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
}

function installRuntimeServer(
  onSend: (socket: TestWebSocket, payload: Record<string, unknown>) => void
) {
  TestWebSocket.instances = []
  TestWebSocket.onSend = onSend
  vi.stubGlobal("WebSocket", TestWebSocket)
}

function runtimeEvent(
  socket: TestWebSocket,
  runId: string,
  sequence: number,
  eventType: string,
  data: Record<string, unknown>
) {
  socket.reply({
    jsonrpc: "2.0",
    method: "run.event",
    params: { runId, sequence, eventType, eventVersion: 1, data },
  })
}

function completeRuntimeRun(
  socket: TestWebSocket,
  request: Record<string, unknown>,
  runId: string,
  answer: string
) {
  socket.reply({
    jsonrpc: "2.0",
    id: request.id,
    result: { runId, status: "running", lastSequence: 1 },
  })
  runtimeEvent(socket, runId, 1, "run.queued", { status: "queued" })
  runtimeEvent(socket, runId, 2, "assistant.delta", { text: answer })
  runtimeEvent(socket, runId, 3, "assistant.completed", { content: answer })
  runtimeEvent(socket, runId, 4, "run.completed", { status: "completed" })
}

function startupFetch(
  extra: (
    url: string,
    init?: RequestInit
  ) => Response | Promise<Response> | undefined
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? "GET"
    const override = extra(url, init)
    if (override) return override
    if (url === "/api/profile" && method === "GET") return response(profile)
    if (url === "/api/settings" && method === "GET") return response(settings)
    if (url === "/api/onboarding" && method === "GET")
      return response(onboardingState)
    if (url === "/api/sessions" && method === "GET") return response([])
    throw new Error(`Unhandled request: ${method} ${url}`)
  })
}

afterEach(() => {
  cleanup()
  toast.dismiss()
  vi.unstubAllGlobals()
  localStorage.clear()
  onboardingState = { current_step: "complete", completed: true }
})

describe("local-first chat", () => {
  it("offers a per-run model choice with two configured models and sends the budget", async () => {
    const configuredSettings = {
      ...settings,
      providers: settings.providers.map((provider) => ({
        ...provider,
        configured: true,
      })),
      models: settings.models.map((model) => ({ ...model, configured: true })),
      default_budget_preset: "conservative",
    }
    const runtimeRequests: Record<string, unknown>[] = []
    installRuntimeServer((socket, request) => {
      runtimeRequests.push(request)
      if (request.method === "run.start") {
        completeRuntimeRun(socket, request, "run-selected-model", "Done")
      }
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url, init) => {
        if (url === "/api/settings") return response(configuredSettings)
        if (url === "/api/sessions" && init?.method === "POST") {
          return response(recentSession, 201)
        }
        if (url === `/api/sessions/${recentSession.id}`) {
          return response({ session: recentSession, messages: [] })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("combobox", { name: "Run model" })
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Run model" }),
      "anthropic:claude-sonnet-5"
    )
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Run budget" }),
      "longer"
    )
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Answer")
    await user.click(screen.getByRole("button", { name: "Send" }))

    await waitFor(() =>
      expect(runtimeRequests[0]).toMatchObject({
        method: "run.start",
        params: {
          modelId: "anthropic:claude-sonnet-5",
          budgetPreset: "longer",
        },
      })
    )
  })

  it("shows the selected model as text when only one model is configured", async () => {
    vi.stubGlobal(
      "fetch",
      startupFetch(() => undefined)
    )
    renderApp()
    expect(await screen.findByText("GPT-5.5")).toBeInTheDocument()
    expect(screen.queryByRole("combobox", { name: "Run model" })).toBeNull()
    expect(
      screen.getByRole("combobox", { name: "Run budget" })
    ).toBeInTheDocument()
  })

  it("saves a default budget in Settings and uses it in the composer", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/settings/budget" && init?.method === "PUT") {
        expect(JSON.parse(String(init.body))).toEqual({
          budget_preset: "longer",
        })
        return response({ ...settings, default_budget_preset: "longer" })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await user.click(await screen.findByRole("button", { name: "Settings" }))
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Default run budget" }),
      "longer"
    )
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/budget",
        expect.objectContaining({ method: "PUT" })
      )
    )
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "Default run budget" })
      ).toHaveValue("longer")
    )
    await user.click(screen.getByRole("button", { name: /New session/ }))
    expect(screen.getByRole("combobox", { name: "Run budget" })).toHaveValue(
      "longer"
    )
  })

  it("attaches an absolute folder to a new session before sending a message", async () => {
    const workspacePath = "/tmp/trellis-project"
    const created = {
      ...recentSession,
      id: "session-workspace",
      title: "New session",
      message_count: 0,
      workspace_path: workspacePath,
    }
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/sessions" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({
          workspace_path: workspacePath,
        })
        return response(created, 201)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach workspace" }))
    await user.type(
      screen.getByRole("textbox", { name: "Workspace folder" }),
      workspacePath
    )
    await user.click(screen.getByRole("button", { name: "Save workspace" }))

    expect(await screen.findByText(workspacePath)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "POST" })
    )
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("keeps the composer idle while a workspace attachment is saving", async () => {
    const saveGate = deferred<void>()
    const workspacePath = "/tmp/trellis-project"
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/sessions" && init?.method === "POST") {
        return saveGate.promise.then(() =>
          response({ ...recentSession, workspace_path: workspacePath }, 201)
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach workspace" }))
    await user.type(
      screen.getByRole("textbox", { name: "Workspace folder" }),
      workspacePath
    )
    await user.click(screen.getByRole("button", { name: "Save workspace" }))

    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    saveGate.resolve()
    expect(
      await screen.findByRole("button", { name: "Change workspace" })
    ).toBeEnabled()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("rejects a relative workspace path without creating a session", async () => {
    const fetchMock = startupFetch(() => undefined)
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach workspace" }))
    await user.type(
      screen.getByRole("textbox", { name: "Workspace folder" }),
      "relative/project"
    )
    await user.click(screen.getByRole("button", { name: "Save workspace" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Enter an absolute folder path."
    )
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "POST" })
    )
  })

  it("changes and removes the folder attached to an existing session", async () => {
    const current = { ...recentSession, workspace_path: "/tmp/first-project" }
    const replacement = "/tmp/second-project"
    const updates: Array<string | null> = []
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/sessions" && !init?.method) return response([current])
      if (url === `/api/sessions/${current.id}` && !init?.method) {
        return response({ session: current, messages: [] })
      }
      if (
        url === `/api/sessions/${current.id}/workspace` &&
        init?.method === "PUT"
      ) {
        const payload = JSON.parse(String(init.body)) as {
          workspace_path: string | null
        }
        updates.push(payload.workspace_path)
        return response({ ...current, workspace_path: payload.workspace_path })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    expect(await screen.findByText("/tmp/first-project")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Change workspace" }))
    await user.clear(screen.getByRole("textbox", { name: "Workspace folder" }))
    await user.type(
      screen.getByRole("textbox", { name: "Workspace folder" }),
      replacement
    )
    await user.click(screen.getByRole("button", { name: "Save workspace" }))
    expect(await screen.findByText(replacement)).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Change workspace" }))
    await user.click(screen.getByRole("button", { name: "Remove workspace" }))
    expect(await screen.findByText("Chat without a folder")).toBeInTheDocument()
    expect(updates).toEqual([replacement, null])
  })

  it("shows onboarding before exposing the workspace on the first visit", async () => {
    const fetchMock = startupFetch(() => undefined)
    vi.stubGlobal("fetch", fetchMock)

    renderFirstRunApp()

    expect(
      await screen.findByRole("heading", { name: "Workspace setup" })
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("textbox", { name: "Message" })
    ).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "GET" })
    )
  })

  it("saves onboarding steps on the server, then restores the workspace", async () => {
    const calls: string[] = []
    let currentSettings = settings
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (method !== "GET") calls.push(`${method} ${url}`)
      if (url === "/api/settings" && method === "GET")
        return response(currentSettings)
      if (url === "/api/onboarding/steps/intro" && method === "PUT")
        return response({ current_step: "profile", completed: false })
      if (url === "/api/onboarding/steps/profile" && method === "PUT")
        return response({ current_step: "model", completed: false })
      if (url === "/api/onboarding/steps/model" && method === "PUT") {
        const submitted = JSON.parse(String(init?.body)) as {
          model_id: string
          api_key: string
        }
        expect(submitted).toEqual({
          model_id: "anthropic:claude-sonnet-5",
          api_key: "sk-ant-draft",
        })
        currentSettings = {
          ...settings,
          selected_provider: "anthropic",
          selected_model_id: submitted.model_id,
          models: settings.models.map((model) =>
            model.id === submitted.model_id
              ? { ...model, configured: true, key_hint: "••••draft" }
              : model
          ),
        }
        return response({ current_step: "complete", completed: true })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderFirstRunApp()
    await user.click(await screen.findByRole("button", { name: "Continue" }))
    await user.clear(screen.getByRole("textbox", { name: "Name" }))
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Ada")
    await user.clear(screen.getByRole("textbox", { name: "Email" }))
    await user.type(
      screen.getByRole("textbox", { name: "Email" }),
      "ada@example.com"
    )
    await user.click(screen.getByRole("button", { name: "Continue" }))
    await user.click(screen.getByRole("radio", { name: /Anthropic/ }))
    await user.type(screen.getByLabelText("Anthropic API key"), "sk-ant-draft")
    await user.click(screen.getByRole("button", { name: "Start Trellis" }))

    expect(
      await screen.findByText("A workspace for ideas in motion")
    ).toBeInTheDocument()
    expect(calls).toEqual([
      "PUT /api/onboarding/steps/intro",
      "PUT /api/onboarding/steps/profile",
      "PUT /api/onboarding/steps/model",
    ])
    expect(currentSettings.selected_model_id).toBe("anthropic:claude-sonnet-5")
  })

  it("keeps onboarding open and does not mark completion when setup fails", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/onboarding/steps/intro" && init?.method === "PUT") {
        return response({ current_step: "profile", completed: false })
      }
      if (url === "/api/onboarding/steps/profile" && init?.method === "PUT") {
        return response(
          {
            error: {
              code: "profile_update_failed",
              message: "The profile could not be saved.",
            },
          },
          500
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderFirstRunApp()
    await user.click(await screen.findByRole("button", { name: "Continue" }))
    await user.click(screen.getByRole("button", { name: "Continue" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The profile could not be saved."
    )
    expect(
      screen.getByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
  })

  it("restores the most recent complete transcript and switches sessions by ID", async () => {
    const fetchMock = startupFetch((url) => {
      if (url === "/api/sessions")
        return response([recentSession, olderSession])
      if (url === "/api/sessions/session-recent") {
        return response({
          session: recentSession,
          messages: [
            {
              id: "message-1",
              turn_id: "turn-1",
              role: "user",
              content: "What survived the restart?",
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
            {
              id: "message-2",
              turn_id: "turn-1",
              role: "assistant",
              content: "The complete local transcript.",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          ],
        })
      }
      if (url === "/api/sessions/session-older") {
        return response({
          session: olderSession,
          messages: [
            {
              id: "message-3",
              turn_id: "turn-2",
              role: "user",
              content: "Older context",
              provider: null,
              model: null,
              created_at: "2026-07-20T10:00:00Z",
            },
          ],
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)

    renderApp()

    expect(
      await screen.findByText("What survived the restart?")
    ).toBeInTheDocument()
    expect(
      screen.getByText("The complete local transcript.")
    ).toBeInTheDocument()
    expect(
      within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText(
        "Persisted conversation"
      )
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))

    expect(await screen.findByText("Older context")).toBeInTheDocument()
    expect(
      within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText(
        "Earlier notes"
      )
    ).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/session-older",
      expect.anything()
    )
  })

  it("ignores a stale session response after a newer session is selected", async () => {
    const olderGate = deferred<void>()
    let recentLoads = 0
    const fetchMock = startupFetch((url) => {
      if (url === "/api/sessions")
        return response([recentSession, olderSession])
      if (url === "/api/sessions/session-recent") {
        recentLoads += 1
        return response({
          session: recentSession,
          messages: [
            {
              id: "recent-message",
              turn_id: "recent-turn",
              role: "assistant",
              content: "Current session content",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          ],
        })
      }
      if (url === "/api/sessions/session-older") {
        return olderGate.promise.then(() =>
          response({
            session: olderSession,
            messages: [
              {
                id: "stale-message",
                turn_id: "stale-turn",
                role: "user",
                content: "Stale session content",
                provider: null,
                model: null,
                created_at: "2026-07-20T10:00:00Z",
              },
            ],
          })
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)

    renderApp()
    expect(
      await screen.findByText("Current session content")
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/sessions/session-older",
        expect.anything()
      )
    )
    await userEvent.click(
      screen.getByRole("button", { name: "Persisted conversation" })
    )
    await waitFor(() => expect(recentLoads).toBe(2))

    await act(async () => {
      olderGate.resolve()
      await olderGate.promise
    })

    expect(screen.getByText("Current session content")).toBeInTheDocument()
    expect(screen.queryByText("Stale session content")).not.toBeInTheDocument()
  })

  it("keeps a new session local until first send, then persists the turn", async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = []
    const runtimeRequests: Record<string, unknown>[] = []
    const createGate = deferred<void>()
    const turnGate = deferred<void>()
    const created = {
      ...recentSession,
      id: "session-created",
      title: "New session",
      message_count: 0,
    }
    const completedSession = {
      ...created,
      title: "Plan the migration",
      message_count: 2,
    }
    installRuntimeServer((socket, request) => {
      runtimeRequests.push(request)
      if (request.method === "run.start") {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-created", status: "running", lastSequence: 1 },
        })
        runtimeEvent(socket, "run-created", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-created", 2, "assistant.delta", {
          text: "Here is",
        })
        void turnGate.promise.then(() => {
          runtimeEvent(socket, "run-created", 3, "assistant.delta", {
            text: " the plan.",
          })
          runtimeEvent(socket, "run-created", 4, "assistant.completed", {
            content: "Here is the plan.",
          })
          runtimeEvent(socket, "run-created", 5, "run.completed", {
            status: "completed",
          })
        })
      }
    })
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (method !== "GET")
        calls.push({ url, method, body: String(init?.body ?? "") })
      if (url === "/api/sessions" && method === "POST")
        return createGate.promise.then(() => response(created, 201))
      if (url === "/api/sessions/session-created" && method === "GET") {
        const request = runtimeRequests[0]
        const params = request?.params as Record<string, unknown> | undefined
        return response({
          session: completedSession,
          messages: [
            {
              id: "message-user",
              turn_id: params?.turnId,
              role: "user",
              content: params?.content,
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
            {
              id: "message-assistant",
              turn_id: params?.turnId,
              role: "assistant",
              content: "Here is the plan.",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          ],
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    expect(calls).toHaveLength(0)

    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Plan the migration"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    await waitFor(() => expect(calls).toHaveLength(1))
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled()

    createGate.resolve()
    expect(
      await screen.findByLabelText("Assistant response streaming")
    ).toHaveTextContent("Here is")
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled()

    turnGate.resolve()
    expect(await screen.findByText("Here is the plan.")).toBeInTheDocument()
    expect(calls.map(({ url, method }) => `${method} ${url}`)).toEqual([
      "POST /api/sessions",
    ])
    expect(runtimeRequests[0]).toMatchObject({
      method: "run.start",
      params: {
        sessionId: "session-created",
        content: "Plan the migration",
      },
    })
  })

  it("sends a JSON-RPC cancellation when Stop generating is clicked", async () => {
    const created = {
      ...recentSession,
      id: "session-cancel",
      title: "New session",
      message_count: 0,
    }
    let streamingSocket: TestWebSocket | null = null
    let submittedTurnId = ""
    installRuntimeServer((socket, request) => {
      if (request.method === "run.start") {
        streamingSocket = socket
        const params = request.params as Record<string, unknown>
        submittedTurnId = String(params.turnId)
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-to-cancel",
            status: "running",
            lastSequence: 1,
          },
        })
        runtimeEvent(socket, "run-to-cancel", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-to-cancel", 2, "assistant.delta", {
          text: "Working on it",
        })
      } else if (request.method === "run.cancel") {
        expect(request.params).toMatchObject({
          runId: "run-to-cancel",
          afterSequence: 2,
        })
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-to-cancel",
            status: "cancelling",
            lastSequence: 3,
          },
        })
        if (streamingSocket) {
          runtimeEvent(streamingSocket, "run-to-cancel", 3, "run.cancelled", {
            code: "user_cancelled",
            message: "The run was cancelled.",
          })
        }
      }
    })
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions" && method === "POST")
        return response(created, 201)
      if (url === "/api/sessions/session-cancel" && method === "GET") {
        return response({
          session: { ...created, message_count: 1 },
          messages: [
            {
              id: "cancelled-user",
              turn_id: submittedTurnId,
              role: "user",
              content: "Stop this run",
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
          ],
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Stop this run"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))
    await screen.findByText("Working on it")
    await user.click(screen.getByRole("button", { name: "Stop generating" }))

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("The run was cancelled.")
    expect(
      within(alert).getByRole("button", { name: "Retry" })
    ).toBeInTheDocument()
  })

  it("does not create an empty session when the selected provider has no key", async () => {
    const fetchMock = startupFetch((url) => {
      if (url === "/api/settings") {
        return response({
          ...settings,
          providers: settings.providers.map((provider) =>
            provider.id === "openai"
              ? { ...provider, configured: false, key_hint: null }
              : provider
          ),
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Do not abandon this draft"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add an API key for OpenAI in Settings."
    )
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "POST" })
    )
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(
      "Do not abandon this draft"
    )
  })

  it("shows a sanitized provider error and retries with the same turn ID", async () => {
    let attempts = 0
    let persistedTurnId = ""
    const requestIds: unknown[] = []
    const created = {
      ...recentSession,
      id: "session-failed",
      title: "New session",
      message_count: 0,
    }
    installRuntimeServer((socket, request) => {
      if (request.method !== "run.start") return
      attempts += 1
      const params = request.params as Record<string, unknown>
      requestIds.push(params.clientRequestId)
      if (!persistedTurnId) persistedTurnId = String(params.turnId)
      expect(params.turnId).toBe(persistedTurnId)
      expect(params.content).toBe("Retry this")
      if (attempts === 1) {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-failed", status: "running", lastSequence: 1 },
        })
        runtimeEvent(socket, "run-failed", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-failed", 2, "run.failed", {
          code: "provider_timeout",
          message: "The provider timed out.",
        })
      } else {
        completeRuntimeRun(socket, request, "run-retried", "Recovered response")
      }
    })
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions" && method === "POST")
        return response(created, 201)
      if (url === "/api/sessions/session-failed") {
        const messages: TestMessage[] = [
          {
            id: "message-user",
            turn_id: persistedTurnId,
            role: "user",
            content: "Retry this",
            provider: null,
            model: null,
            created_at: "2026-08-25T10:00:00Z",
          },
        ]
        if (attempts > 1) {
          messages.push({
            id: "message-assistant",
            turn_id: persistedTurnId,
            role: "assistant",
            content: "Recovered response",
            provider: "openai",
            model: "gpt-5.5",
            created_at: "2026-08-25T10:00:01Z",
          })
        }
        return response({
          session: {
            ...created,
            title: "Retry this",
            message_count: messages.length,
          },
          messages,
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Retry this"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("The provider timed out.")
    expect(alert).not.toHaveTextContent("sk-")

    await user.click(within(alert).getByRole("button", { name: "Retry" }))

    expect(await screen.findByText("Recovered response")).toBeInTheDocument()
    expect(attempts).toBe(2)
    expect(requestIds[0]).not.toBe(requestIds[1])
  })

  it("reconstructs a same-ID retry from an unmatched persisted user message", async () => {
    const persistedTurnId = "e49ea024-e340-4c85-a7a6-f8c8459a9811"
    let detailLoads = 0
    installRuntimeServer((socket, request) => {
      if (request.method !== "run.start") return
      expect(request.params).toMatchObject({
        sessionId: "session-recent",
        turnId: persistedTurnId,
        content: "Recover after restart",
      })
      completeRuntimeRun(
        socket,
        request,
        "run-recovered",
        "Recovered after restart"
      )
    })
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions") return response([recentSession])
      if (url === "/api/sessions/session-recent" && method === "GET") {
        detailLoads += 1
        const messages: TestMessage[] = [
          {
            id: "persisted-user",
            turn_id: persistedTurnId,
            role: "user",
            content: "Recover after restart",
            provider: null,
            model: null,
            created_at: "2026-08-25T10:00:00Z",
          },
        ]
        if (detailLoads > 1) {
          messages.push({
            id: "recovered-assistant",
            turn_id: persistedTurnId,
            role: "assistant",
            content: "Recovered after restart",
            provider: "openai",
            model: "gpt-5.5",
            created_at: "2026-08-25T10:00:01Z",
          })
        }
        return response({
          session: { ...recentSession, message_count: messages.length },
          messages,
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)

    renderApp()
    expect(await screen.findByText("Recover after restart")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Retry" }))

    expect(
      await screen.findByText("Recovered after restart")
    ).toBeInTheDocument()
  })

  it("edits the local profile and treats provider keys as write-only", async () => {
    let currentProfile = profile
    let currentSettings = settings
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/profile" && method === "PUT") {
        currentProfile = {
          ...currentProfile,
          ...(JSON.parse(String(init?.body)) as object),
        }
        return response(currentProfile)
      }
      if (
        url === "/api/settings/providers/openai/api-key" &&
        method === "PUT"
      ) {
        expect(JSON.parse(String(init?.body))).toEqual({
          api_key: "sk-new-secret-4567",
        })
        currentSettings = {
          ...currentSettings,
          providers: currentSettings.providers.map((provider) =>
            provider.id === "openai"
              ? { ...provider, configured: true, key_hint: "••••4567" }
              : provider
          ),
        }
        return response(currentSettings)
      }
      if (
        url === "/api/settings/providers/openai/api-key" &&
        method === "DELETE"
      ) {
        currentSettings = {
          ...currentSettings,
          providers: currentSettings.providers.map((provider) =>
            provider.id === "openai"
              ? { ...provider, configured: false, key_hint: null }
              : provider
          ),
        }
        return response(currentSettings)
      }
      if (url === "/api/settings/model" && method === "PUT") {
        const { model_id } = JSON.parse(String(init?.body)) as {
          model_id: string
        }
        currentSettings = {
          ...currentSettings,
          selected_provider: "anthropic",
          selected_model_id: model_id,
        }
        return response(currentSettings)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    await user.click(screen.getByRole("button", { name: "Settings" }))

    expect(screen.getByDisplayValue(profile.id)).toBeDisabled()
    const displayName = screen.getByRole("textbox", { name: "Display name" })
    await user.clear(displayName)
    await user.type(displayName, "Ada Lovelace")
    await user.click(screen.getByRole("button", { name: "Save profile" }))
    await waitFor(() =>
      expect(currentProfile.display_name).toBe("Ada Lovelace")
    )
    expect(
      within(screen.getByRole("region", { name: "Notifications" })).getByText(
        "Profile saved"
      )
    ).toBeInTheDocument()
    const profileToast = screen
      .getByText("Profile saved")
      .closest("[data-sonner-toast]")
    expect(profileToast).not.toBeNull()
    expect(getComputedStyle(profileToast!).alignItems).toBe("center")
    const profileIcon = profileToast!.querySelector("[data-icon]")
    expect(profileIcon).not.toBeNull()
    expect(screen.queryByText("Saved locally.")).not.toBeInTheDocument()

    const keyInput = screen.getByLabelText("OpenAI API key")
    await user.type(keyInput, "sk-new-secret-4567")
    await user.click(screen.getByRole("button", { name: "Save OpenAI key" }))

    await waitFor(() => expect(keyInput).toHaveValue(""))
    expect(screen.getByText("OpenAI key saved")).toBeInTheDocument()
    expect(screen.getByText("Configured · ••••4567")).toBeInTheDocument()
    expect(
      screen.queryByDisplayValue("sk-new-secret-4567")
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Remove key" }))
    expect(
      await screen.findByText(
        "Not configured. The key is write-only and will not be shown again."
      )
    ).toBeInTheDocument()
    expect(screen.getByText("OpenAI key removed")).toBeInTheDocument()

    const anthropic = screen.getByRole("radio", { name: /Anthropic/ })
    await user.click(anthropic)
    await waitFor(() =>
      expect(anthropic).toHaveAttribute("aria-checked", "true")
    )
    expect(screen.getByText("Claude Sonnet 5 selected")).toBeInTheDocument()
  })

  it("shows settings failures in the global notification region", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/profile" && init?.method === "PUT") {
        return response(
          {
            error: {
              code: "profile_update_failed",
              message: "The profile could not be saved.",
            },
          },
          500
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)

    renderApp()
    await screen.findByText("A workspace for ideas in motion")
    await userEvent.click(screen.getByRole("button", { name: "Settings" }))
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }))

    const notifications = screen.getByRole("region", {
      name: "Notifications",
    })
    expect(
      await within(notifications).findByText("Change not saved")
    ).toBeInTheDocument()
    expect(
      within(notifications).getByText("The profile could not be saved.")
    ).toBeInTheDocument()
  })
})
