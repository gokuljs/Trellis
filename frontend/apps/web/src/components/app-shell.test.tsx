import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AppShell } from "@/components/app-shell"
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
}

const recentSession = {
  id: "session-recent",
  title: "Persisted conversation",
  created_at: "2026-08-24T10:00:00Z",
  updated_at: "2026-08-25T10:00:00Z",
  message_count: 2,
}

const olderSession = {
  id: "session-older",
  title: "Earlier notes",
  created_at: "2026-07-20T10:00:00Z",
  updated_at: "2026-07-20T10:00:00Z",
  message_count: 1,
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="dark">
      <AppShell />
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
    if (url === "/api/sessions" && method === "GET") return response([])
    throw new Error(`Unhandled request: ${method} ${url}`)
  })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe("local-first chat", () => {
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

    await userEvent.click(screen.getByRole("button", { name: "Earlier notes" }))

    expect(await screen.findByText("Older context")).toBeInTheDocument()
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
    const createGate = deferred<void>()
    const turnGate = deferred<void>()
    const created = {
      ...recentSession,
      id: "session-created",
      title: "New session",
      message_count: 0,
    }
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (method !== "GET")
        calls.push({ url, method, body: String(init?.body ?? "") })
      if (url === "/api/sessions" && method === "POST")
        return createGate.promise.then(() => response(created, 201))
      if (url === "/api/sessions/session-created/turns" && method === "POST") {
        const submitted = JSON.parse(String(init?.body)) as {
          turn_id: string
          content: string
        }
        const turnResponse = response(
          {
            session: {
              ...created,
              title: "Plan the migration",
              message_count: 2,
            },
            user_message: {
              id: "message-user",
              turn_id: submitted.turn_id,
              role: "user",
              content: submitted.content,
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
            assistant_message: {
              id: "message-assistant",
              turn_id: submitted.turn_id,
              role: "assistant",
              content: "Here is the plan.",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          },
          201
        )
        return turnGate.promise.then(() => turnResponse)
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
      await screen.findByLabelText("Assistant response pending")
    ).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled()

    turnGate.resolve()
    expect(await screen.findByText("Here is the plan.")).toBeInTheDocument()
    expect(calls.map(({ url, method }) => `${method} ${url}`)).toEqual([
      "POST /api/sessions",
      "POST /api/sessions/session-created/turns",
    ])
    expect(JSON.parse(calls[1].body ?? "{}")).toMatchObject({
      content: "Plan the migration",
    })
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
    const created = {
      ...recentSession,
      id: "session-failed",
      title: "New session",
      message_count: 0,
    }
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions" && method === "POST")
        return response(created, 201)
      if (url === "/api/sessions/session-failed/turns" && method === "POST") {
        attempts += 1
        const submitted = JSON.parse(String(init?.body)) as {
          turn_id: string
          content: string
        }
        if (!persistedTurnId) persistedTurnId = submitted.turn_id
        expect(submitted.turn_id).toBe(persistedTurnId)
        if (attempts === 1) {
          return response(
            {
              error: {
                code: "provider_timeout",
                message: "The provider timed out.",
              },
            },
            504
          )
        }
        return response(
          {
            session: { ...created, title: "Retry this", message_count: 2 },
            user_message: {
              id: "message-user",
              turn_id: submitted.turn_id,
              role: "user",
              content: submitted.content,
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
            assistant_message: {
              id: "message-assistant",
              turn_id: submitted.turn_id,
              role: "assistant",
              content: "Recovered response",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          },
          201
        )
      }
      if (url === "/api/sessions/session-failed") {
        return response({
          session: { ...created, title: "Retry this", message_count: 1 },
          messages: [
            {
              id: "message-user",
              turn_id: persistedTurnId,
              role: "user",
              content: "Retry this",
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
      "Retry this"
    )
    await user.click(screen.getByRole("button", { name: "Send" }))

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("The provider timed out.")
    expect(alert).not.toHaveTextContent("sk-")

    await user.click(within(alert).getByRole("button", { name: "Retry" }))

    expect(await screen.findByText("Recovered response")).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it("reconstructs a same-ID retry from an unmatched persisted user message", async () => {
    const persistedTurnId = "e49ea024-e340-4c85-a7a6-f8c8459a9811"
    const fetchMock = startupFetch((url, init) => {
      const method = init?.method ?? "GET"
      if (url === "/api/sessions") return response([recentSession])
      if (url === "/api/sessions/session-recent" && method === "GET") {
        return response({
          session: { ...recentSession, message_count: 1 },
          messages: [
            {
              id: "persisted-user",
              turn_id: persistedTurnId,
              role: "user",
              content: "Recover after restart",
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
          ],
        })
      }
      if (url === "/api/sessions/session-recent/turns" && method === "POST") {
        const submitted = JSON.parse(String(init?.body)) as {
          turn_id: string
          content: string
        }
        expect(submitted).toEqual({
          turn_id: persistedTurnId,
          content: "Recover after restart",
        })
        return response(
          {
            session: recentSession,
            user_message: {
              id: "persisted-user",
              turn_id: submitted.turn_id,
              role: "user",
              content: submitted.content,
              provider: null,
              model: null,
              created_at: "2026-08-25T10:00:00Z",
            },
            assistant_message: {
              id: "recovered-assistant",
              turn_id: submitted.turn_id,
              role: "assistant",
              content: "Recovered after restart",
              provider: "openai",
              model: "gpt-5.5",
              created_at: "2026-08-25T10:00:01Z",
            },
          },
          201
        )
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
      if (url === "/api/settings/provider" && method === "PUT") {
        currentSettings = {
          ...currentSettings,
          selected_provider: "anthropic",
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

    const keyInput = screen.getByLabelText("OpenAI API key")
    await user.type(keyInput, "sk-new-secret-4567")
    await user.click(screen.getByRole("button", { name: "Save OpenAI key" }))

    await waitFor(() => expect(keyInput).toHaveValue(""))
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

    const anthropic = screen.getByRole("radio", { name: /Anthropic/ })
    await user.click(anthropic)
    await waitFor(() =>
      expect(anthropic).toHaveAttribute("aria-checked", "true")
    )
  })
})
