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

function sidebarSessionTitles() {
  return Array.from(document.querySelectorAll(".session-row")).map((row) =>
    row.getAttribute("aria-label")
  )
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
    if (url.endsWith("/runs/latest") && method === "GET") return response(null)
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
  it("shows an approval preview, answers it, and keeps the ordered tool result in chat", async () => {
    const created = {
      ...recentSession,
      id: "session-approval",
      message_count: 0,
      workspace_path: "/tmp/trellis-project",
    }
    let runSocket: TestWebSocket | null = null
    let turnId = ""
    const decisionGate = deferred<void>()
    const requests: Record<string, unknown>[] = []
    installRuntimeServer((socket, request) => {
      requests.push(request)
      if (request.method === "run.start") {
        runSocket = socket
        turnId = String((request.params as Record<string, unknown>).turnId)
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-approval",
            status: "running",
            lastSequence: 0,
            budgetPreset: "conservative",
            limits: {
              maxModelCalls: 8,
              maxToolCalls: 16,
              maxTotalTokens: 100000,
              maxCostUsd: 2,
              deadlineAt: "2026-10-07T12:10:00Z",
            },
          },
        })
        runtimeEvent(socket, "run-approval", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-approval", 2, "run.started", {
          status: "running",
        })
        runtimeEvent(socket, "run-approval", 3, "model.usage", {
          input_tokens: 20,
          output_tokens: 10,
          total_tokens: 30,
          total_estimated_cost_usd: 0.001,
        })
        runtimeEvent(socket, "run-approval", 4, "model.completed", {})
        runtimeEvent(socket, "run-approval", 5, "tool.call", {
          tool_call_id: "tool-patch",
          name: "apply_patch",
          arguments: { path: "src/main.ts" },
        })
        runtimeEvent(socket, "run-approval", 6, "tool.approval_requested", {
          tool_call_id: "tool-patch",
          name: "apply_patch",
          preview: { path: "src/main.ts", diff: "-old\n+new" },
        })
      } else if (request.method === "run.respond") {
        expect(request.params).toMatchObject({
          runId: "run-approval",
          toolCallId: "tool-patch",
          decision: "approved",
          afterSequence: 6,
        })
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-approval",
            status: "waiting_for_approval",
            toolCallId: "tool-patch",
            decision: "approved",
          },
        })
        if (!runSocket) throw new Error("Run socket missing")
        const eventSocket = runSocket
        void decisionGate.promise.then(() => {
          runtimeEvent(
            eventSocket,
            "run-approval",
            7,
            "tool.approval_decided",
            {
              tool_call_id: "tool-patch",
              decision: "approved",
            }
          )
          runtimeEvent(eventSocket, "run-approval", 8, "run.resumed", {
            tool_call_id: "tool-patch",
          })
          runtimeEvent(eventSocket, "run-approval", 9, "tool.result", {
            tool_call_id: "tool-patch",
            status: "completed",
            content: "Patch applied.",
          })
          runtimeEvent(eventSocket, "run-approval", 10, "model.usage", {
            input_tokens: 40,
            output_tokens: 10,
            total_tokens: 80,
            total_estimated_cost_usd: 0.002,
          })
          runtimeEvent(eventSocket, "run-approval", 11, "model.completed", {})
          runtimeEvent(eventSocket, "run-approval", 12, "assistant.delta", {
            text: "Updated.",
          })
          runtimeEvent(eventSocket, "run-approval", 13, "assistant.completed", {
            content: "Updated.",
          })
          runtimeEvent(eventSocket, "run-approval", 14, "run.completed", {
            status: "completed",
          })
        })
      }
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url, init) => {
        if (url === "/api/sessions" && init?.method === "POST")
          return response(created, 201)
        if (
          url === "/api/sessions/session-approval" &&
          (init?.method ?? "GET") === "GET"
        ) {
          return response({
            session: { ...created, message_count: 2 },
            messages: [
              {
                id: "user-approval",
                turn_id: turnId,
                role: "user",
                content: "Update it",
                provider: null,
                model: null,
                created_at: "2026-10-07T12:00:00Z",
              },
              {
                id: "assistant-approval",
                turn_id: turnId,
                role: "assistant",
                content: "Updated.",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-10-07T12:00:01Z",
              },
            ],
          })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Update it"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    const approval = await screen.findByRole("region", {
      name: "Approval required for apply_patch",
    })
    expect(within(approval).getByText(/\+new/)).toBeInTheDocument()
    expect(screen.getByText("30 / 100,000 tokens")).toBeInTheDocument()
    await user.click(within(approval).getByRole("button", { name: "Approve" }))
    await waitFor(() =>
      expect(
        within(approval).getByRole("button", { name: "Approve" })
      ).toBeDisabled()
    )
    decisionGate.resolve()

    expect(await screen.findByText("Patch applied.")).toBeInTheDocument()
    expect(await screen.findByText("Updated.")).toBeInTheDocument()
    expect(screen.getByText("80 / 100,000 tokens")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Approve" })
    ).not.toBeInTheDocument()
    expect(requests.map((request) => request.method)).toEqual([
      "run.start",
      "run.respond",
    ])
  })

  it("accepts a second approval before the first response RPC returns", async () => {
    const created = {
      ...recentSession,
      id: "session-two-approvals",
      message_count: 0,
    }
    let runSocket: TestWebSocket | null = null
    let turnId = ""
    let firstResponse: {
      socket: TestWebSocket
      request: Record<string, unknown>
    } | null = null
    const decisions: Record<string, unknown>[] = []
    installRuntimeServer((socket, request) => {
      if (request.method === "run.start") {
        runSocket = socket
        turnId = String((request.params as Record<string, unknown>).turnId)
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-two-approvals",
            status: "running",
            lastSequence: 0,
          },
        })
        runtimeEvent(
          socket,
          "run-two-approvals",
          1,
          "tool.approval_requested",
          {
            tool_call_id: "tool-1",
            name: "run_command",
            preview: { command: "git status" },
          }
        )
        return
      }
      if (request.method !== "run.respond") return
      decisions.push(request)
      if (decisions.length === 1) {
        firstResponse = { socket, request }
        if (!runSocket) throw new Error("Run socket missing")
        runtimeEvent(
          runSocket,
          "run-two-approvals",
          2,
          "tool.approval_decided",
          {
            tool_call_id: "tool-1",
            decision: "approved",
          }
        )
        runtimeEvent(runSocket, "run-two-approvals", 3, "tool.result", {
          tool_call_id: "tool-1",
          status: "completed",
          content: "First command done.",
        })
        runtimeEvent(
          runSocket,
          "run-two-approvals",
          4,
          "tool.approval_requested",
          {
            tool_call_id: "tool-2",
            name: "run_command",
            preview: { command: "git diff" },
          }
        )
        return
      }
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-two-approvals", status: "waiting_for_approval" },
      })
      if (!runSocket || !firstResponse)
        throw new Error("Approval sockets missing")
      firstResponse.socket.reply({
        jsonrpc: "2.0",
        id: firstResponse.request.id,
        result: { runId: "run-two-approvals", status: "waiting_for_approval" },
      })
      runtimeEvent(runSocket, "run-two-approvals", 5, "tool.approval_decided", {
        tool_call_id: "tool-2",
        decision: "approved",
      })
      runtimeEvent(runSocket, "run-two-approvals", 6, "assistant.delta", {
        text: "Done.",
      })
      runtimeEvent(runSocket, "run-two-approvals", 7, "assistant.completed", {
        content: "Done.",
      })
      runtimeEvent(runSocket, "run-two-approvals", 8, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url, init) => {
        if (url === "/api/sessions" && init?.method === "POST")
          return response(created, 201)
        if (url === "/api/sessions/session-two-approvals")
          return response({
            session: { ...created, message_count: 2 },
            messages: [
              {
                id: "user-two-approvals",
                turn_id: turnId,
                role: "user",
                content: "Run two commands",
                provider: null,
                model: null,
                created_at: "2026-10-07T12:00:00Z",
              },
              {
                id: "assistant-two-approvals",
                turn_id: turnId,
                role: "assistant",
                content: "Done.",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-10-07T12:00:01Z",
              },
            ],
          })
        return undefined
      })
    )
    const user = userEvent.setup()
    renderApp()
    await user.type(
      await screen.findByRole("textbox", { name: "Message" }),
      "Run two commands"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))
    const firstApproval = await screen.findByRole("region", {
      name: "Approval required for run_command",
    })
    await user.click(
      within(firstApproval).getByRole("button", { name: "Approve" })
    )
    const secondApproval = (
      await screen.findByText("git diff")
    ).closest<HTMLElement>('[role="region"]')
    if (!secondApproval) throw new Error("Second approval missing")
    await user.click(
      within(secondApproval).getByRole("button", { name: "Approve" })
    )
    await waitFor(() => expect(decisions).toHaveLength(2))
    expect(
      decisions.map(
        (item) => (item.params as Record<string, unknown>).toolCallId
      )
    ).toEqual(["tool-1", "tool-2"])
    expect(await screen.findByText("Done.")).toBeInTheDocument()
  })

  it("keeps earlier turn activity beside its answer after another run", async () => {
    const created = {
      ...recentSession,
      id: "session-history",
      message_count: 0,
    }
    const turnIds: string[] = []
    let completedRuns = 0
    installRuntimeServer((socket, request) => {
      if (request.method !== "run.start") return
      turnIds.push(String((request.params as Record<string, unknown>).turnId))
      const runNumber = turnIds.length
      const runId = `run-${runNumber}`
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId, status: "running", lastSequence: 0 },
      })
      if (runNumber === 1) {
        runtimeEvent(socket, runId, 1, "tool.call", {
          tool_call_id: "tool-1",
          name: "read_file",
          arguments: { path: "src/main.ts" },
        })
        runtimeEvent(socket, runId, 2, "tool.result", {
          tool_call_id: "tool-1",
          status: "completed",
          content: "First tool output",
        })
      }
      completedRuns = runNumber
      runtimeEvent(socket, runId, runNumber === 1 ? 3 : 1, "assistant.delta", {
        text: runNumber === 1 ? "First answer" : "Second answer",
      })
      runtimeEvent(
        socket,
        runId,
        runNumber === 1 ? 4 : 2,
        "assistant.completed",
        {
          content: runNumber === 1 ? "First answer" : "Second answer",
        }
      )
      runtimeEvent(socket, runId, runNumber === 1 ? 5 : 3, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url, init) => {
        if (url === "/api/sessions" && init?.method === "POST")
          return response(created, 201)
        if (url === "/api/sessions/session-history") {
          const messages: TestMessage[] = []
          for (let index = 0; index < completedRuns; index += 1) {
            messages.push({
              id: `user-${index}`,
              turn_id: turnIds[index],
              role: "user",
              content: index === 0 ? "First prompt" : "Second prompt",
              provider: null,
              model: null,
              created_at: "2026-10-07T12:00:00Z",
            })
            messages.push({
              id: `assistant-${index}`,
              turn_id: turnIds[index],
              role: "assistant",
              content: index === 0 ? "First answer" : "Second answer",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-10-07T12:00:01Z",
            })
          }
          return response({
            session: { ...created, message_count: messages.length },
            messages,
          })
        }
        return undefined
      })
    )
    const user = userEvent.setup()
    renderApp()
    const composer = await screen.findByRole("textbox", { name: "Message" })
    await user.type(composer, "First prompt")
    await user.click(screen.getByRole("button", { name: "Send" }))
    expect(await screen.findByText("First answer")).toBeInTheDocument()
    expect(screen.getByText("First tool output")).toBeInTheDocument()

    await user.type(composer, "Second prompt")
    await user.click(screen.getByRole("button", { name: "Send" }))
    expect(await screen.findByText("Second answer")).toBeInTheDocument()
    const firstResult = screen.getByText("First tool output")
    const firstAnswer = screen.getByText("First answer")
    expect(
      firstResult.compareDocumentPosition(firstAnswer) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()
    const activities = screen.getAllByRole("region", { name: "Run activity" })
    expect(activities).toHaveLength(2)
    expect(
      within(activities[0]).getByText("First tool output")
    ).toBeInTheDocument()
    expect(
      firstAnswer.compareDocumentPosition(activities[1]) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()
  })

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
    const runBudgetSelect = screen.getByRole("combobox", { name: "Run budget" })
    expect(
      within(runBudgetSelect).getByRole("option", { name: "Standard" })
    ).toBeInTheDocument()
    expect(
      within(runBudgetSelect).getByRole("option", { name: "Extended" })
    ).toBeInTheDocument()
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
    const defaultBudgetSelect = screen.getByRole("combobox", {
      name: "Default run budget",
    })
    expect(
      within(defaultBudgetSelect).getByRole("option", { name: "Standard" })
    ).toBeInTheDocument()
    expect(
      within(defaultBudgetSelect).getByRole("option", { name: "Extended" })
    ).toBeInTheDocument()
    await user.selectOptions(defaultBudgetSelect, "longer")
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

  it("opens and dismisses the composer attachment menu", async () => {
    vi.stubGlobal(
      "fetch",
      startupFetch(() => undefined)
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    expect(await screen.findByRole("menu")).toBeInTheDocument()

    await user.click(screen.getByRole("textbox", { name: "Message" }))
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.keyboard("{Escape}")
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.keyboard("{Tab}")
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus()
  })

  it("supports arrow-key navigation in the composer attachment menu", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response(
          {
            error: {
              code: "workspace_picker_unavailable",
              message: "Trellis could not open the folder picker.",
            },
          },
          503
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    const workspaceItem = await screen.findByRole("menuitem", {
      name: "Workspace",
    })
    await user.click(workspaceItem)
    const manualEntry = await screen.findByRole("menuitem", {
      name: "Enter path manually",
    })

    await user.keyboard("{ArrowDown}")
    expect(manualEntry).toHaveFocus()
    await user.keyboard("{ArrowUp}")
    expect(workspaceItem).toHaveFocus()
  })

  it("attaches a picked folder to a new session immediately", async () => {
    const workspacePath = "/tmp/trellis-project"
    const created = {
      ...recentSession,
      id: "session-workspace",
      title: "New session",
      message_count: 0,
      workspace_path: workspacePath,
    }
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response({ path: workspacePath })
      }
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
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))

    const workspaceChip = await screen.findByText("trellis-project")
    expect(workspaceChip.closest(".composer-box")).not.toBeNull()
    expect(screen.queryByText(workspacePath)).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("")
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/workspaces/pick",
      expect.objectContaining({ method: "POST" })
    )
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "POST" })
    )
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("keeps the composer idle while the folder picker and attachment are pending", async () => {
    const pickGate = deferred<Response>()
    const workspacePath = "/tmp/trellis-project"
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return pickGate.promise
      }
      if (url === "/api/sessions" && init?.method === "POST") {
        return response(
          { ...recentSession, workspace_path: workspacePath },
          201
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))

    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Opening folder…"
    )
    pickGate.resolve(response({ path: workspacePath }))
    expect(await screen.findByText("trellis-project")).toBeInTheDocument()
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("offers manual path entry when the folder picker is unavailable", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response(
          {
            error: {
              code: "workspace_picker_unavailable",
              message: "Trellis could not open the folder picker.",
            },
          },
          503
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Trellis could not open the folder picker."
    )
    await user.click(
      screen.getByRole("menuitem", { name: "Enter path manually" })
    )
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

  it("does not offer manual entry when the folder picker times out", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response(
          {
            error: {
              code: "workspace_picker_timeout",
              message: "The folder picker took too long to respond.",
            },
          },
          504
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The folder picker took too long to respond."
    )
    expect(
      screen.queryByRole("menuitem", { name: "Enter path manually" })
    ).not.toBeInTheDocument()
  })

  it("preserves a manual path draft when a picker retry fails", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response(
          {
            error: {
              code: "workspace_picker_unavailable",
              message: "Trellis could not open the folder picker.",
            },
          },
          503
        )
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))
    await user.click(
      await screen.findByRole("menuitem", { name: "Enter path manually" })
    )
    const pathInput = screen.getByRole("textbox", { name: "Workspace folder" })
    await user.type(pathInput, "/tmp/manual-draft")
    await user.click(screen.getByRole("button", { name: "Choose folder…" }))

    expect(
      screen.getByRole("textbox", { name: "Workspace folder" })
    ).toHaveValue("/tmp/manual-draft")
  })

  it("preserves, replaces, and removes a workspace on an existing session", async () => {
    const current = { ...recentSession, workspace_path: "/tmp/first-project" }
    const replacement = "/tmp/second-project"
    let pickerCalls = 0
    const updates: Array<string | null> = []
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        pickerCalls += 1
        return response({ path: pickerCalls === 1 ? null : replacement })
      }
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
    await screen.findByRole("textbox", { name: "Message" })
    const currentChip = await screen.findByText("first-project")
    expect(currentChip.closest(".composer-box")).not.toBeNull()
    expect(screen.queryByText(current.workspace_path!)).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))
    expect(screen.getByText("first-project")).toBeInTheDocument()
    expect(updates).toEqual([])

    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))
    const replacementChip = await screen.findByText("second-project")
    expect(replacementChip.closest(".composer-box")).not.toBeNull()
    expect(screen.queryByText(replacement)).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Remove workspace" }))
    expect(screen.queryByText("second-project")).not.toBeInTheDocument()
    expect(updates).toEqual([replacement, null])
  })

  it("leaves a new chat unchanged when the folder picker is cancelled", async () => {
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/workspaces/pick" && init?.method === "POST") {
        return response({ path: null })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))

    expect(screen.queryByRole("menu")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Attach" })).toHaveFocus()
    expect(screen.queryByText("trellis-project")).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/sessions",
      expect.objectContaining({ method: "POST" })
    )
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
      await screen.findByRole("textbox", { name: "Message" })
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

  it("keeps sidebar order when selecting a session with a completed run", async () => {
    const resumeMethods: string[] = []
    installRuntimeServer((socket, request) => {
      resumeMethods.push(String(request.method))
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-older-complete",
          status: "completed",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "run-older-complete", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "run-older-complete", 2, "run.started", {
        status: "running",
      })
      runtimeEvent(socket, "run-older-complete", 3, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions")
          return response([recentSession, olderSession])
        if (url === "/api/sessions/session-older/runs/latest")
          return response({
            run_id: "run-older-complete",
            turn_id: "older-turn",
            status: "completed",
            last_sequence: 2,
          })
        if (url === "/api/sessions/session-older")
          return response({
            session: olderSession,
            messages: [
              {
                id: "older-answer",
                turn_id: "older-turn",
                role: "assistant",
                content: "Older context",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-07-20T10:00:01Z",
              },
            ],
          })
        return undefined
      })
    )

    renderApp()
    expect(
      await screen.findByRole("button", { name: "Earlier notes" })
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))
    expect(await screen.findByText("Older context")).toBeInTheDocument()

    await waitFor(() =>
      expect(
        Array.from(document.querySelectorAll(".session-row")).map((row) =>
          row.getAttribute("aria-label")
        )
      ).toEqual(["Persisted conversation", "Earlier notes"])
    )
    expect(resumeMethods).toEqual([])
  })

  it("keeps sidebar order when selection refreshes a missing run transcript", async () => {
    let olderDetailLoads = 0
    const latestTurnId = "latest-older-turn"
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions")
          return response([recentSession, olderSession])
        if (url === "/api/sessions/session-recent")
          return response({
            session: recentSession,
            messages: [
              {
                id: "recent-answer",
                turn_id: "recent-turn",
                role: "assistant",
                content: "Recent context",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-08-25T10:00:01Z",
              },
            ],
          })
        if (url === "/api/sessions/session-older/runs/latest")
          return response({
            run_id: "run-older-complete",
            turn_id: latestTurnId,
            status: "completed",
            last_sequence: 3,
          })
        if (url === "/api/sessions/session-older") {
          olderDetailLoads += 1
          const messages: TestMessage[] = [
            {
              id: "older-context",
              turn_id: "older-context-turn",
              role: "assistant",
              content: "Older context",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-07-20T10:00:00Z",
            },
          ]
          if (olderDetailLoads > 1)
            messages.push({
              id: "latest-older-answer",
              turn_id: latestTurnId,
              role: "assistant",
              content: "Recovered older answer",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-07-20T10:00:01Z",
            })
          return response({ session: olderSession, messages })
        }
        return undefined
      })
    )

    renderApp()
    await screen.findByRole("button", { name: "Earlier notes" })
    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))

    expect(
      await screen.findByText("Recovered older answer")
    ).toBeInTheDocument()
    expect(olderDetailLoads).toBe(2)
    expect(sidebarSessionTitles()).toEqual([
      "Persisted conversation",
      "Earlier notes",
    ])
  })

  it("keeps sidebar order when a selected chat's recovered run finishes", async () => {
    const activeTurnId = "older-active-turn"
    let runtimeSocket: TestWebSocket | null = null
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.resume")
      runtimeSocket = socket
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "older-active-run",
          status: "running",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "older-active-run", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "older-active-run", 2, "run.started", {
        status: "running",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions")
          return response([recentSession, olderSession])
        if (url === "/api/sessions/session-recent")
          return response({ session: recentSession, messages: [] })
        if (url === "/api/sessions/session-older/runs/latest")
          return response({
            run_id: "older-active-run",
            turn_id: activeTurnId,
            status: "running",
            last_sequence: 2,
          })
        if (url === "/api/sessions/session-older")
          return response({
            session: olderSession,
            messages: [
              {
                id: "older-active-user",
                turn_id: activeTurnId,
                role: "user",
                content: "Continue the older chat",
                provider: null,
                model: null,
                created_at: "2026-07-20T10:00:00Z",
              },
            ],
          })
        return undefined
      })
    )

    renderApp()
    await screen.findByRole("button", { name: "Earlier notes" })
    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))
    expect(
      await screen.findByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()

    await act(async () => {
      if (!runtimeSocket) throw new Error("the older run was not resumed")
      runtimeEvent(runtimeSocket, "older-active-run", 3, "run.completed", {
        status: "completed",
      })
    })

    await waitFor(() =>
      expect(sidebarSessionTitles()).toEqual([
        "Persisted conversation",
        "Earlier notes",
      ])
    )
  })

  it("keeps a chat in place when a new message is sent", async () => {
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.start")
      expect(request.params).toMatchObject({ content: "A new message" })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "new-older-turn", status: "running", lastSequence: 2 },
      })
      runtimeEvent(socket, "new-older-turn", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "new-older-turn", 2, "run.started", {
        status: "running",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions")
          return response([recentSession, olderSession])
        if (url === "/api/sessions/session-recent")
          return response({ session: recentSession, messages: [] })
        if (url === "/api/sessions/session-older")
          return response({
            session: olderSession,
            messages: [
              {
                id: "older-context",
                turn_id: "older-context-turn",
                role: "assistant",
                content: "Older context",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-07-20T10:00:00Z",
              },
            ],
          })
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("button", { name: "Earlier notes" })
    await user.click(screen.getByRole("button", { name: "Earlier notes" }))
    await screen.findByText("Older context")
    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "A new message"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    expect(
      await screen.findByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()
    expect(sidebarSessionTitles()).toEqual([
      "Persisted conversation",
      "Earlier notes",
    ])
  })

  it("keeps a retried older chat in place", async () => {
    const failedTurnId = "older-failed-turn"
    let retryCount = 0
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.start")
      expect(request.params).toMatchObject({
        sessionId: "session-older",
        turnId: failedTurnId,
        content: "Retry the older request",
      })
      retryCount += 1
      completeRuntimeRun(socket, request, "older-retry", "Recovered response")
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions")
          return response([recentSession, olderSession])
        if (url === "/api/sessions/session-recent")
          return response({ session: recentSession, messages: [] })
        if (url === "/api/sessions/session-older/runs/latest")
          return response(null)
        if (url === "/api/sessions/session-older") {
          const messages: TestMessage[] = [
            {
              id: "older-failed-user",
              turn_id: failedTurnId,
              role: "user",
              content: "Retry the older request",
              provider: null,
              model: null,
              created_at: "2026-07-20T10:00:00Z",
            },
          ]
          if (retryCount > 0) {
            messages.push({
              id: "older-retried-answer",
              turn_id: failedTurnId,
              role: "assistant",
              content: "Recovered response",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-07-20T10:00:01Z",
            })
          }
          return response({ session: olderSession, messages })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("button", { name: "Earlier notes" })
    await user.click(screen.getByRole("button", { name: "Earlier notes" }))
    const alert = await screen.findByRole("alert")
    await user.click(within(alert).getByRole("button", { name: "Retry" }))

    expect(await screen.findByText("Recovered response")).toBeInTheDocument()
    expect(retryCount).toBe(1)
    expect(sidebarSessionTitles()).toEqual([
      "Persisted conversation",
      "Earlier notes",
    ])
  })

  it("keeps an older chat in place when its workspace changes", async () => {
    const current = { ...olderSession, workspace_path: "/tmp/first-project" }
    const updated = { ...current, workspace_path: "/tmp/second-project" }
    const fetchMock = startupFetch((url, init) => {
      if (url === "/api/sessions" && !init?.method)
        return response([recentSession, current])
      if (url === "/api/sessions/session-recent")
        return response({ session: recentSession, messages: [] })
      if (url === `/api/sessions/${current.id}` && !init?.method)
        return response({ session: current, messages: [] })
      if (url === "/api/workspaces/pick" && init?.method === "POST")
        return response({ path: updated.workspace_path })
      if (
        url === `/api/sessions/${current.id}/workspace` &&
        init?.method === "PUT"
      )
        return response(updated)
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)
    const user = userEvent.setup()

    renderApp()
    await screen.findByRole("textbox", { name: "Message" })
    await user.click(screen.getByRole("button", { name: "Earlier notes" }))
    await screen.findByText("first-project")
    await user.click(screen.getByRole("button", { name: "Attach" }))
    await user.click(screen.getByRole("menuitem", { name: "Workspace" }))

    expect(await screen.findByText("second-project")).toBeInTheDocument()
    expect(sidebarSessionTitles()).toEqual([
      "Persisted conversation",
      "Earlier notes",
    ])
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
    await screen.findByRole("textbox", { name: "Message" })
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
    await screen.findByRole("textbox", { name: "Message" })
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
    await screen.findByRole("textbox", { name: "Message" })
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
    await screen.findByRole("textbox", { name: "Message" })
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

  it("discovers and replays an active run before offering Retry after refresh", async () => {
    const turnId = "8a7fb1b8-8280-4a02-9292-487fd605e81e"
    let completed = false
    let runtimeSocket: TestWebSocket | null = null
    const methods: string[] = []
    installRuntimeServer((socket, request) => {
      methods.push(String(request.method))
      expect(request).toMatchObject({
        method: "run.resume",
        params: { runId: "run-already-active", afterSequence: 0 },
      })
      runtimeSocket = socket
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-already-active",
          status: "running",
          lastSequence: 3,
        },
      })
      runtimeEvent(socket, "run-already-active", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "run-already-active", 2, "run.started", {
        status: "running",
      })
      runtimeEvent(socket, "run-already-active", 3, "assistant.delta", {
        text: "Working",
      })
    })
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions") return response([recentSession])
      if (url === "/api/sessions/session-recent/runs/latest")
        return response({
          run_id: "run-already-active",
          turn_id: turnId,
          status: "running",
          last_sequence: 3,
        })
      if (url === "/api/sessions/session-recent" && method === "GET") {
        const messages: TestMessage[] = [
          {
            id: "persisted-user",
            turn_id: turnId,
            role: "user",
            content: "Continue this run",
            provider: null,
            model: null,
            created_at: "2026-08-25T10:00:00Z",
          },
        ]
        if (completed)
          messages.push({
            id: "persisted-answer",
            turn_id: turnId,
            role: "assistant",
            content: "Done after refresh",
            provider: "openai",
            model: "gpt-5.5",
            created_at: "2026-08-25T10:00:01Z",
          })
        return response({
          session: { ...recentSession, message_count: messages.length },
          messages,
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetchMock)

    renderApp()
    expect(await screen.findByText("Working")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(
      screen.getByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()
    expect(methods).toEqual(["run.resume"])

    completed = true
    act(() => {
      if (!runtimeSocket) throw new Error("run was not resumed")
      runtimeEvent(runtimeSocket, "run-already-active", 4, "run.completed", {
        status: "completed",
      })
    })

    expect(await screen.findByText("Done after refresh")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
  })

  it("blocks duplicate runs when the first latest-run lookup fails", async () => {
    const turnId = "049a301b-eb21-4fc9-bf73-a0dc3ab3bd0a"
    let lookupUnavailable = true
    let completed = false
    const methods: string[] = []
    installRuntimeServer((socket, request) => {
      methods.push(String(request.method))
      expect(request.method).toBe("run.resume")
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-lookup-recovery",
          status: "running",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "run-lookup-recovery", 1, "run.queued", {
        status: "queued",
      })
      completed = true
      runtimeEvent(socket, "run-lookup-recovery", 2, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest") {
          if (lookupUnavailable) throw new Error("Local service is offline")
          return response({
            run_id: "run-lookup-recovery",
            turn_id: turnId,
            status: completed ? "completed" : "running",
            last_sequence: completed ? 2 : 0,
          })
        }
        if (url === "/api/sessions/session-recent") {
          const messages: TestMessage[] = [
            {
              id: "persisted-user",
              turn_id: turnId,
              role: "user",
              content: "Resume after lookup failure",
              provider: null,
              model: null,
              created_at: "2026-10-07T12:00:00Z",
            },
          ]
          if (completed)
            messages.push({
              id: "persisted-answer",
              turn_id: turnId,
              role: "assistant",
              content: "Recovered after lookup failure",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-10-07T12:00:01Z",
            })
          return response({
            session: { ...recentSession, message_count: messages.length },
            messages,
          })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    const reconnect = await screen.findByRole("button", {
      name: "Reconnect",
    })
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Stop generating" })).toBeNull()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(methods).toEqual([])

    lookupUnavailable = false
    await user.click(reconnect)
    expect(
      await screen.findByText("Recovered after lookup failure")
    ).toBeInTheDocument()
    expect(methods).toEqual(["run.resume"])
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("offers Retry after reconnection confirms no run exists", async () => {
    const turnId = "58cf8d40-0ac7-4bef-89a6-461461d27879"
    let lookupUnavailable = true
    installRuntimeServer(() => {
      throw new Error("No run should be resumed")
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest") {
          if (lookupUnavailable) throw new Error("Local service is offline")
          return response(null)
        }
        if (url === "/api/sessions/session-recent")
          return response({
            session: { ...recentSession, message_count: 1 },
            messages: [
              {
                id: "persisted-user",
                turn_id: turnId,
                role: "user",
                content: "No run started",
                provider: null,
                model: null,
                created_at: "2026-10-07T12:00:00Z",
              },
            ],
          })
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    const reconnect = await screen.findByRole("button", {
      name: "Reconnect",
    })
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    lookupUnavailable = false
    await user.click(reconnect)

    expect(
      await screen.findByRole("button", { name: "Retry" })
    ).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("restores an actionable approval and its ordered result after refresh", async () => {
    const turnId = "c345edbf-f3a4-4a0a-8067-c408d15bed55"
    let completed = false
    let detailLoads = 0
    let runSocket: TestWebSocket | null = null
    const methods: string[] = []
    installRuntimeServer((socket, request) => {
      methods.push(String(request.method))
      if (request.method === "run.resume") {
        expect(request.params).toMatchObject({
          runId: "run-refresh-approval",
          afterSequence: 0,
        })
        runSocket = socket
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-refresh-approval",
            status: "waiting_for_approval",
            lastSequence: 4,
            budgetPreset: "conservative",
            limits: {
              maxModelCalls: 8,
              maxToolCalls: 16,
              maxTotalTokens: 100000,
              maxCostUsd: 2,
              deadlineAt: "2026-10-07T12:10:00Z",
            },
          },
        })
        runtimeEvent(socket, "run-refresh-approval", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-refresh-approval", 2, "run.started", {
          status: "running",
        })
        runtimeEvent(socket, "run-refresh-approval", 3, "tool.call", {
          tool_call_id: "tool-refresh-patch",
          name: "apply_patch",
          arguments: { path: "src/main.ts" },
        })
        runtimeEvent(
          socket,
          "run-refresh-approval",
          4,
          "tool.approval_requested",
          {
            tool_call_id: "tool-refresh-patch",
            name: "apply_patch",
            preview: { path: "src/main.ts", diff: "-old\n+new" },
          }
        )
      } else if (request.method === "run.respond") {
        expect(request.params).toMatchObject({
          runId: "run-refresh-approval",
          toolCallId: "tool-refresh-patch",
          decision: "approved",
          afterSequence: 4,
        })
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-refresh-approval",
            status: "waiting_for_approval",
            toolCallId: "tool-refresh-patch",
            decision: "approved",
          },
        })
        if (!runSocket) throw new Error("Restored run socket missing")
        runtimeEvent(
          runSocket,
          "run-refresh-approval",
          5,
          "tool.approval_decided",
          { tool_call_id: "tool-refresh-patch", decision: "approved" }
        )
        runtimeEvent(runSocket, "run-refresh-approval", 6, "run.resumed", {
          tool_call_id: "tool-refresh-patch",
        })
        runtimeEvent(runSocket, "run-refresh-approval", 7, "tool.result", {
          tool_call_id: "tool-refresh-patch",
          status: "completed",
          content: "Patch applied after refresh.",
        })
        completed = true
        runtimeEvent(runSocket, "run-refresh-approval", 8, "run.completed", {
          status: "completed",
        })
      }
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest")
          return response({
            run_id: "run-refresh-approval",
            turn_id: turnId,
            status: completed ? "completed" : "waiting_for_approval",
            last_sequence: completed ? 8 : 4,
          })
        if (url === "/api/sessions/session-recent") {
          detailLoads += 1
          if (detailLoads === 1)
            return response({
              session: { ...recentSession, message_count: 2 },
              messages: [
                {
                  id: "earlier-user",
                  turn_id: "earlier-turn",
                  role: "user",
                  content: "Earlier request",
                  provider: null,
                  model: null,
                  created_at: "2026-10-07T11:00:00Z",
                },
                {
                  id: "earlier-answer",
                  turn_id: "earlier-turn",
                  role: "assistant",
                  content: "Earlier answer",
                  provider: "openai",
                  model: "gpt-5.5",
                  created_at: "2026-10-07T11:00:01Z",
                },
              ],
            })
          const messages: TestMessage[] = [
            {
              id: "persisted-user",
              turn_id: turnId,
              role: "user",
              content: "Patch this file",
              provider: null,
              model: null,
              created_at: "2026-10-07T12:00:00Z",
            },
          ]
          if (completed)
            messages.push({
              id: "persisted-answer",
              turn_id: turnId,
              role: "assistant",
              content: "Patch complete.",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-10-07T12:00:01Z",
            })
          return response({
            session: { ...recentSession, message_count: messages.length },
            messages,
          })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    const approval = await screen.findByRole("region", {
      name: "Approval required for apply_patch",
    })
    expect(detailLoads).toBeGreaterThanOrEqual(2)
    expect(within(approval).getByText(/\+new/)).toBeInTheDocument()
    expect(screen.getByText("0 / 8 model calls")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    await user.click(within(approval).getByRole("button", { name: "Approve" }))

    expect(
      await screen.findByText("Patch applied after refresh.")
    ).toBeInTheDocument()
    expect(await screen.findByText("Patch complete.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull()
    expect(methods).toEqual(["run.resume", "run.respond"])
  })

  it("reconnects a discovered active run after its first replay connection fails", async () => {
    const turnId = "7087cb08-dd55-4405-a928-2f23c15848df"
    let attempts = 0
    let completed = false
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.resume")
      expect(request.params).toMatchObject({
        runId: "run-recovery",
        afterSequence: 0,
      })
      attempts += 1
      if (attempts === 1) {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          error: {
            code: -32000,
            message: "Connection to Trellis was lost.",
            data: { code: "connection_lost" },
          },
        })
        return
      }
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-recovery", status: "running", lastSequence: 3 },
      })
      runtimeEvent(socket, "run-recovery", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "run-recovery", 2, "assistant.delta", {
        text: "Recovered",
      })
      completed = true
      runtimeEvent(socket, "run-recovery", 3, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest")
          return response({
            run_id: "run-recovery",
            turn_id: turnId,
            status: completed ? "completed" : "running",
            last_sequence: completed ? 3 : 1,
          })
        if (url === "/api/sessions/session-recent") {
          const messages: TestMessage[] = [
            {
              id: "persisted-user",
              turn_id: turnId,
              role: "user",
              content: "Recover this",
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
          ]
          if (completed)
            messages.push({
              id: "persisted-answer",
              turn_id: turnId,
              role: "assistant",
              content: "Recovered answer",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            })
          return response({
            session: { ...recentSession, message_count: messages.length },
            messages,
          })
        }
        return undefined
      })
    )

    renderApp()
    expect(await screen.findByText("Recovered answer")).toBeInTheDocument()
    expect(attempts).toBe(2)
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Stop generating" })).toBeNull()
  })

  it("finishes cancellation after a restored stream loses its connection", async () => {
    const turnId = "b502668b-7d27-4298-89b2-4e18864aae0a"
    const retryLookup = deferred<Response>()
    let lookups = 0
    let resumeAttempts = 0
    let cancelled = false
    installRuntimeServer((socket, request) => {
      if (request.method === "run.cancel") {
        expect(request.params).toMatchObject({
          runId: "run-cancel-after-loss",
          afterSequence: 0,
        })
        cancelled = true
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-cancel-after-loss", status: "cancelling" },
        })
        return
      }
      expect(request.method).toBe("run.resume")
      resumeAttempts += 1
      if (resumeAttempts === 1) {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          error: {
            code: -32000,
            message: "Connection to Trellis was lost.",
            data: { code: "connection_lost" },
          },
        })
        return
      }
      expect(cancelled).toBe(true)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-cancel-after-loss",
          status: "cancelled",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "run-cancel-after-loss", 1, "run.queued", {
        status: "queued",
      })
      runtimeEvent(socket, "run-cancel-after-loss", 2, "run.cancelled", {
        code: "user_cancelled",
        message: "The run was cancelled.",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest") {
          lookups += 1
          if (lookups === 2) return retryLookup.promise
          return response({
            run_id: "run-cancel-after-loss",
            turn_id: turnId,
            status: cancelled ? "cancelled" : "running",
            last_sequence: cancelled ? 2 : 0,
          })
        }
        if (url === "/api/sessions/session-recent")
          return response({
            session: { ...recentSession, message_count: 1 },
            messages: [
              {
                id: "persisted-user",
                turn_id: turnId,
                role: "user",
                content: "Stop after connection loss",
                provider: null,
                model: null,
                created_at: "2026-08-25T10:00:00Z",
              },
            ],
          })
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    await screen.findByText("Reconnecting to Trellis…")
    await user.click(screen.getByRole("button", { name: "Stop generating" }))
    expect(cancelled).toBe(true)
    retryLookup.resolve(
      response({
        run_id: "run-cancel-after-loss",
        turn_id: turnId,
        status: "cancelled",
        last_sequence: 2,
      })
    )

    const alert = await screen.findByRole("alert")
    await waitFor(() =>
      expect(alert).toHaveTextContent("The run was cancelled.")
    )
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeEnabled()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
    expect(resumeAttempts).toBe(2)
  })

  it("offers manual reconnection after a bounded restored-run outage", async () => {
    const turnId = "187819a4-3696-4b45-a242-39d3bf3939a7"
    let attempts = 0
    let serviceReturned = false
    let lookupUnavailable = false
    let completed = false
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.resume")
      attempts += 1
      if (!serviceReturned) {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          error: {
            code: -32000,
            message: "Connection to Trellis was lost.",
            data: { code: "connection_lost" },
          },
        })
        return
      }
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-outage",
          status: "running",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "run-outage", 1, "run.queued", {
        status: "queued",
      })
      completed = true
      runtimeEvent(socket, "run-outage", 2, "run.completed", {
        status: "completed",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest") {
          if (lookupUnavailable) throw new Error("Local service is offline")
          return response({
            run_id: "run-outage",
            turn_id: turnId,
            status: completed ? "completed" : "running",
            last_sequence: completed ? 2 : 0,
          })
        }
        if (url === "/api/sessions/session-recent") {
          const messages: TestMessage[] = [
            {
              id: "persisted-user",
              turn_id: turnId,
              role: "user",
              content: "Recover after an outage",
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
          ]
          if (completed)
            messages.push({
              id: "persisted-answer",
              turn_id: turnId,
              role: "assistant",
              content: "Recovered after outage",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            })
          return response({
            session: { ...recentSession, message_count: messages.length },
            messages,
          })
        }
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    const reconnect = await screen.findByRole(
      "button",
      { name: "Reconnect" },
      { timeout: 3_000 }
    )
    expect(attempts).toBe(4)
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    lookupUnavailable = true
    await user.click(reconnect)
    expect(
      await screen.findByRole("button", { name: "Reconnect" })
    ).toBeInTheDocument()
    serviceReturned = true
    lookupUnavailable = false
    await user.click(screen.getByRole("button", { name: "Reconnect" }))
    expect(
      await screen.findByText("Recovered after outage")
    ).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
  })

  it("hides stale approval actions while rediscovering a run", async () => {
    const turnId = "bb03fa3a-65fd-4190-bb76-045b8d5fcda0"
    let resumes = 0
    let lookupUnavailable = false
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.resume")
      resumes += 1
      if (resumes === 1) {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-stale-approval",
            status: "waiting_for_approval",
            lastSequence: 3,
          },
        })
        runtimeEvent(socket, "run-stale-approval", 1, "run.queued", {
          status: "queued",
        })
        runtimeEvent(socket, "run-stale-approval", 2, "tool.call", {
          tool_call_id: "tool-stale",
          name: "apply_patch",
        })
        runtimeEvent(
          socket,
          "run-stale-approval",
          3,
          "tool.approval_requested",
          { tool_call_id: "tool-stale", name: "apply_patch" }
        )
        setTimeout(() => socket.close(), 50)
        return
      }
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        error: {
          code: -32000,
          message: "Connection to Trellis was lost.",
          data: { code: "connection_lost" },
        },
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest") {
          if (lookupUnavailable) throw new Error("Local service is offline")
          return response({
            run_id: "run-stale-approval",
            turn_id: turnId,
            status: "waiting_for_approval",
            last_sequence: 3,
          })
        }
        if (url === "/api/sessions/session-recent")
          return response({
            session: { ...recentSession, message_count: 1 },
            messages: [
              {
                id: "persisted-user",
                turn_id: turnId,
                role: "user",
                content: "Patch this file",
                provider: null,
                model: null,
                created_at: "2026-10-07T12:00:00Z",
              },
            ],
          })
        return undefined
      })
    )
    const user = userEvent.setup()

    renderApp()
    const approval = await screen.findByRole("region", {
      name: "Approval required for apply_patch",
    })
    expect(
      within(approval).getByRole("button", { name: "Approve" })
    ).toBeEnabled()
    const reconnect = await screen.findByRole(
      "button",
      { name: "Reconnect" },
      { timeout: 3_000 }
    )
    lookupUnavailable = true
    await user.click(reconnect)

    expect(
      await screen.findByRole("button", { name: "Reconnect" })
    ).toBeInTheDocument()
    expect(
      within(approval).queryByRole("button", { name: "Approve" })
    ).toBeNull()
    expect(within(approval).queryByRole("button", { name: "Deny" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(resumes).toBe(4)
  })

  it.each(["queued", "waiting_for_approval"])(
    "keeps Retry hidden while a restored run is %s",
    async (initialStatus) => {
      const turnId = "a369bf43-07d8-4b5b-a128-f058d24bb87f"
      const replay =
        initialStatus === "queued"
          ? [["run.queued", { status: "queued" }]]
          : [
              ["run.queued", { status: "queued" }],
              ["run.started", { status: "running" }],
              [
                "tool.call",
                { tool_call_id: "tool-approval", name: "read_file" },
              ],
              [
                "tool.approval_requested",
                { tool_call_id: "tool-approval", name: "read_file" },
              ],
            ]
      let latestStatus = initialStatus
      let runtimeSocket: TestWebSocket | null = null
      installRuntimeServer((socket, request) => {
        expect(request.method).toBe("run.resume")
        runtimeSocket = socket
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-pending-refresh",
            status: initialStatus,
            lastSequence: replay.length,
          },
        })
        replay.forEach(([eventType, data], index) => {
          runtimeEvent(
            socket,
            "run-pending-refresh",
            index + 1,
            eventType as string,
            data as Record<string, unknown>
          )
        })
      })
      vi.stubGlobal(
        "fetch",
        startupFetch((url) => {
          if (url === "/api/sessions") return response([recentSession])
          if (url === "/api/sessions/session-recent/runs/latest")
            return response({
              run_id: "run-pending-refresh",
              turn_id: turnId,
              status: latestStatus,
              last_sequence: replay.length,
            })
          if (url === "/api/sessions/session-recent")
            return response({
              session: { ...recentSession, message_count: 1 },
              messages: [
                {
                  id: "persisted-user",
                  turn_id: turnId,
                  role: "user",
                  content: "Wait for this run",
                  provider: null,
                  model: null,
                  created_at: "2026-08-25T10:00:00Z",
                },
              ],
            })
          return undefined
        })
      )

      renderApp()
      expect(
        await screen.findByRole("button", { name: "Stop generating" })
      ).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
      expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()

      if (initialStatus === "waiting_for_approval") {
        const user = userEvent.setup()
        await user.click(screen.getByRole("button", { name: /New session/ }))
        expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
        latestStatus = "cancelled"
        act(() => {
          if (!runtimeSocket) throw new Error("run was not resumed")
          runtimeEvent(
            runtimeSocket,
            "run-pending-refresh",
            replay.length + 1,
            "run.cancelled",
            { status: "cancelled" }
          )
        })
        expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled()
        return
      }

      latestStatus = "cancelled"
      act(() => {
        if (!runtimeSocket) throw new Error("run was not resumed")
        runtimeEvent(
          runtimeSocket,
          "run-pending-refresh",
          replay.length + 1,
          "run.cancelled",
          { status: "cancelled" }
        )
      })
      expect(
        await screen.findByRole("button", { name: "Retry" })
      ).toBeInTheDocument()
    }
  )

  it("waits for a saved failure event before showing Retry after refresh", async () => {
    const turnId = "0cbac0bb-d031-4ef2-a7e1-cc006487e756"
    let runtimeSocket: TestWebSocket | null = null
    installRuntimeServer((socket, request) => {
      expect(request.method).toBe("run.resume")
      runtimeSocket = socket
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-failed-before-refresh",
          status: "failed",
          lastSequence: 2,
        },
      })
      runtimeEvent(socket, "run-failed-before-refresh", 1, "run.queued", {
        status: "queued",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest")
          return response({
            run_id: "run-failed-before-refresh",
            turn_id: turnId,
            status: "failed",
            last_sequence: 2,
          })
        if (url === "/api/sessions/session-recent")
          return response({
            session: { ...recentSession, message_count: 1 },
            messages: [
              {
                id: "persisted-user",
                turn_id: turnId,
                role: "user",
                content: "Try this again",
                provider: null,
                model: null,
                created_at: "2026-08-25T10:00:00Z",
              },
            ],
          })
        return undefined
      })
    )

    renderApp()
    expect(
      await screen.findByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()

    act(() => {
      if (!runtimeSocket) throw new Error("failed run was not replayed")
      runtimeEvent(
        runtimeSocket,
        "run-failed-before-refresh",
        2,
        "run.failed",
        {
          code: "provider_timeout",
          message: "The provider timed out.",
        }
      )
    })

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("The provider timed out.")
    expect(
      within(alert).getByRole("button", { name: "Retry" })
    ).toBeInTheDocument()
  })

  it("starts new work without replaying a completed run from the restored timeline", async () => {
    const turnId = "bc3f4014-2e95-424e-988d-3060d7087fc3"
    const methods: string[] = []
    installRuntimeServer((socket, request) => {
      methods.push(String(request.method))
      expect(request.method).toBe("run.start")
      expect(request.params).toMatchObject({
        sessionId: "session-recent",
        content: "Next request",
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-new", status: "running", lastSequence: 2 },
      })
      runtimeEvent(socket, "run-new", 1, "run.queued", { status: "queued" })
      runtimeEvent(socket, "run-new", 2, "run.started", {
        status: "running",
      })
    })
    vi.stubGlobal(
      "fetch",
      startupFetch((url) => {
        if (url === "/api/sessions") return response([recentSession])
        if (url === "/api/sessions/session-recent/runs/latest")
          return response({
            run_id: "run-complete",
            turn_id: turnId,
            status: "completed",
            last_sequence: 3,
          })
        if (url === "/api/sessions/session-recent")
          return response({
            session: recentSession,
            messages: [
              {
                id: "saved-user",
                turn_id: turnId,
                role: "user",
                content: "A finished request",
                provider: null,
                model: null,
                created_at: "2026-08-25T10:00:00Z",
              },
              {
                id: "saved-answer",
                turn_id: turnId,
                role: "assistant",
                content: "Saved answer",
                provider: "openai",
                model: "gpt-5.5",
                created_at: "2026-08-25T10:00:01Z",
              },
            ],
          })
        return undefined
      })
    )

    const user = userEvent.setup()
    renderApp()
    expect(await screen.findByText("Saved answer")).toBeInTheDocument()
    expect(methods).toEqual([])
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()

    await user.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Next request"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))
    expect(
      await screen.findByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()
    expect(methods).toEqual(["run.start"])
    expect(
      screen.getByRole("button", { name: "Stop generating" })
    ).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(screen.getByText("Next request")).toBeInTheDocument()
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
    await screen.findByRole("textbox", { name: "Message" })
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
    await screen.findByRole("textbox", { name: "Message" })
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
