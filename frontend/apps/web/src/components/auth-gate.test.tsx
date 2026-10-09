import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useState } from "react"
import type { User } from "@supabase/supabase-js"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { App } from "@/App"
import { AuthGate } from "@/components/auth-gate"
import { ThemeProvider } from "@/components/theme-provider"
import type { AuthState } from "@/lib/auth-controller"

const auth = vi.hoisted(() => ({
  state: {
    status: "loading",
    user: null,
    error: null,
    pendingProvider: null,
  } as AuthState,
  listeners: new Set<() => void>(),
  signIn: vi.fn(async () => undefined),
  retry: vi.fn(async () => undefined),
  signOut: vi.fn(async () => undefined),
}))

vi.mock("@/lib/auth-controller", () => ({
  getAuthController: () => ({
    getSnapshot: () => auth.state,
    subscribe: (listener: () => void) => {
      auth.listeners.add(listener)
      return () => auth.listeners.delete(listener)
    },
    signIn: auth.signIn,
    retry: auth.retry,
    signOut: auth.signOut,
  }),
}))

function update(state: Partial<AuthState>) {
  act(() => {
    auth.state = { ...auth.state, ...state }
    for (const listener of auth.listeners) listener()
  })
}

beforeEach(() => {
  auth.state = {
    status: "loading",
    user: null,
    error: null,
    pendingProvider: null,
  }
  auth.signIn.mockClear()
  auth.retry.mockClear()
  auth.signOut.mockClear()
})

afterEach(() => {
  cleanup()
  auth.listeners.clear()
  vi.unstubAllGlobals()
})

describe("authentication gate", () => {
  it.each(["loading", "onboarding"])(
    "lets a verified user sign out during workspace %s",
    async (stage) => {
      auth.state = {
        ...auth.state,
        status: "signed-in",
        user: { id: "account-1", email: "ada@example.com" } as User,
      }
      const fetch = vi.fn((url: string) => {
        if (stage === "loading") return new Promise<Response>(() => {})
        const data =
          url === "/api/profile"
            ? { id: "profile-1", display_name: null, email: null }
            : url === "/api/settings"
              ? { providers: [], models: [] }
              : { completed: false, current_step: "intro" }
        return Promise.resolve(
          new Response(JSON.stringify(data), {
            headers: { "Content-Type": "application/json" },
          })
        )
      })
      vi.stubGlobal("fetch", fetch)
      render(
        <ThemeProvider>
          <App />
        </ThemeProvider>
      )
      if (stage === "onboarding") {
        expect(
          await screen.findByRole("heading", { name: "Workspace setup" })
        ).toBeVisible()
      }

      await userEvent.click(screen.getByLabelText("Account: ada@example.com"))
      await userEvent.click(screen.getByRole("button", { name: "Sign out" }))

      expect(auth.signOut).toHaveBeenCalledOnce()
    }
  )

  it("preserves workspace state on token refresh and resets it for a different account", async () => {
    const user = userEvent.setup()
    auth.state = {
      ...auth.state,
      status: "signed-in",
      user: { id: "account-1" } as User,
    }
    function Workspace() {
      const [count, setCount] = useState(0)
      return <button onClick={() => setCount(count + 1)}>Draft {count}</button>
    }
    render(
      <AuthGate>
        <Workspace />
      </AuthGate>
    )
    await user.click(screen.getByRole("button", { name: "Draft 0" }))
    expect(screen.getByRole("button", { name: "Draft 1" })).toBeVisible()

    update({ user: { id: "account-1" } as User })
    expect(screen.getByRole("button", { name: "Draft 1" })).toBeVisible()

    update({ user: { id: "account-2" } as User })
    expect(screen.getByRole("button", { name: "Draft 0" })).toBeVisible()
  })
  it("makes no backend requests while restoring authentication or signed out", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValue(new Error("Unexpected backend call"))
    vi.stubGlobal("fetch", fetch)
    render(
      <ThemeProvider>
        <App />
      </ThemeProvider>
    )

    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking your session"
    )
    expect(fetch).not.toHaveBeenCalled()

    update({ status: "signed-out" })

    expect(
      screen.getByRole("heading", { name: "Sign in to Trellis" })
    ).toBeVisible()
    expect(
      screen.getByRole("button", { name: "Continue with Google" })
    ).toBeEnabled()
    expect(
      screen.getByRole("button", { name: "Continue with GitHub" })
    ).toBeEnabled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it("mounts the workspace only after a verified user is available", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("Backend unavailable"))
    vi.stubGlobal("fetch", fetch)
    render(
      <ThemeProvider>
        <App />
      </ThemeProvider>
    )

    update({ status: "signed-in", user: { id: "account-1" } as User })

    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(fetch.mock.calls.map(([url]) => String(url))).toContain(
      "/api/profile"
    )
    expect(
      screen.queryByRole("heading", { name: "Sign in to Trellis" })
    ).not.toBeInTheDocument()
  })

  it.each(["Google", "GitHub"])(
    "starts the selected %s provider",
    async (provider) => {
      const user = userEvent.setup()
      auth.state = { ...auth.state, status: "signed-out" }
      render(
        <AuthGate>
          <p>Workspace</p>
        </AuthGate>
      )

      await user.click(
        screen.getByRole("button", { name: `Continue with ${provider}` })
      )

      expect(auth.signIn).toHaveBeenCalledWith(provider.toLowerCase())
      expect(screen.queryByText("Workspace")).not.toBeInTheDocument()
    }
  )

  it("prevents duplicate sign-in while redirecting to a provider", () => {
    auth.state = {
      ...auth.state,
      status: "signed-out",
      pendingProvider: "github",
    }
    render(
      <AuthGate>
        <p>Workspace</p>
      </AuthGate>
    )

    expect(
      screen.getByRole("button", { name: "Continue with Google" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: /Connecting to GitHub/ })
    ).toBeDisabled()
  })

  it("shows a configuration error without a sign-in bypass", () => {
    auth.state = {
      ...auth.state,
      status: "configuration-error",
      error:
        "Configure VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY to enable sign-in.",
    }
    render(
      <AuthGate>
        <p>Workspace</p>
      </AuthGate>
    )

    expect(screen.getByRole("alert")).toHaveTextContent("VITE_SUPABASE_URL")
    expect(screen.queryByText("Workspace")).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Continue with Google" })
    ).not.toBeInTheDocument()
  })

  it("lets a user retry session validation after a network failure", async () => {
    const user = userEvent.setup()
    auth.state = {
      ...auth.state,
      status: "error",
      error: "Your session could not be checked. Try again.",
    }
    render(
      <AuthGate>
        <p>Workspace</p>
      </AuthGate>
    )

    expect(screen.getByRole("alert")).toHaveTextContent(
      "session could not be checked"
    )
    await user.click(screen.getByRole("button", { name: "Try again" }))

    expect(auth.retry).toHaveBeenCalledOnce()
  })

  it("shows a cancelled OAuth error while keeping both providers available", () => {
    auth.state = {
      ...auth.state,
      status: "signed-out",
      error: "Sign-in was cancelled. Choose a provider to try again.",
    }
    render(
      <AuthGate>
        <p>Workspace</p>
      </AuthGate>
    )

    expect(screen.getByRole("alert")).toHaveTextContent("Sign-in was cancelled")
    expect(
      screen.getByRole("button", { name: "Continue with Google" })
    ).toBeEnabled()
  })

  it("unmounts workspace state when the account signs out", () => {
    auth.state = {
      ...auth.state,
      status: "signed-in",
      user: { id: "account-1" } as User,
    }
    const unmounted = vi.fn()
    function Workspace() {
      return (
        <p
          ref={(element) => {
            if (!element) unmounted()
          }}
        >
          Private workspace
        </p>
      )
    }
    render(
      <AuthGate>
        <Workspace />
      </AuthGate>
    )
    expect(screen.getByText("Private workspace")).toBeVisible()

    update({ status: "signed-out", user: null })

    expect(screen.queryByText("Private workspace")).not.toBeInTheDocument()
    expect(unmounted).toHaveBeenCalled()
  })
})
