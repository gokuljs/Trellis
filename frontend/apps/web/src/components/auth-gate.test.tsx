import type {
  AuthChangeEvent,
  Session,
  SupabaseClient,
  User,
} from "@supabase/supabase-js"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AuthGate } from "@/components/auth-gate"
import {
  createAuthController,
  type AuthController,
} from "@/lib/auth-controller"

const currentAuth = vi.hoisted(() => ({
  controller: null as AuthController | null,
}))

vi.mock("@/lib/auth-controller", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/auth-controller")>()
  return { ...actual, getAuthController: () => currentAuth.controller! }
})

const user: User = {
  id: "user-1",
  aud: "authenticated",
  email: "ada@example.com",
  app_metadata: { provider: "google" },
  user_metadata: { full_name: "Ada" },
  created_at: "2026-10-10T00:00:00Z",
}

const session: Session = {
  access_token: "test-access-token",
  refresh_token: "test-refresh-token",
  expires_in: 3600,
  token_type: "bearer",
  user,
}

function renderAuth(path: string, restoredSession: Session | null = null) {
  window.history.replaceState(null, "", path)
  let authChanged: (
    event: AuthChangeEvent,
    session: Session | null
  ) => void = () => {}
  const oauth = vi.fn().mockResolvedValue({
    data: { provider: "google", url: "https://provider.example/authorize" },
    error: null,
  })
  const sdk = {
    auth: {
      getSession: vi
        .fn()
        .mockResolvedValue({ data: { session: restoredSession }, error: null }),
      getUser: vi.fn().mockResolvedValue({ data: { user }, error: null }),
      exchangeCodeForSession: vi.fn().mockResolvedValue({
        data: { session, user },
        error: null,
      }),
      signInWithOAuth: oauth,
      signOut: vi.fn().mockResolvedValue({ error: null }),
      onAuthStateChange: (callback: typeof authChanged) => {
        authChanged = callback
        return { data: { subscription: { unsubscribe: () => {} } } }
      },
    },
  }
  const navigate = vi.fn()
  currentAuth.controller = createAuthController(
    sdk as unknown as SupabaseClient,
    navigate
  )
  render(
    <AuthGate>
      <p>Workspace content</p>
    </AuthGate>
  )
  return {
    sdk,
    navigate,
    authChanged: (event: AuthChangeEvent, next: Session | null) =>
      authChanged(event, next),
  }
}

afterEach(() => {
  cleanup()
  window.history.replaceState(null, "", "/")
})

describe("auth page routes", () => {
  it.each([
    ["/login", "Sign in to Trellis", "Sign-in options"],
    ["/signup", "Create your Trellis account", "Sign-up options"],
  ])("opens the correct auth page at %s", async (path, heading, options) => {
    renderAuth(path)

    expect(
      await screen.findByRole("heading", { name: heading })
    ).toBeInTheDocument()
    expect(screen.getByRole("group", { name: options })).toBeInTheDocument()
    expect(window.location.pathname).toBe(path)
    expect(screen.queryByText("Workspace content")).not.toBeInTheDocument()
  })

  it("redirects a signed-out workspace visit to /login", async () => {
    renderAuth("/")

    await screen.findByRole("heading", { name: "Sign in to Trellis" })
    await waitFor(() => expect(window.location.pathname).toBe("/login"))
  })

  it("links login and signup and follows browser Back and Forward", async () => {
    renderAuth("/login")
    await screen.findByRole("heading", { name: "Sign in to Trellis" })
    const signup = screen.getByRole("link", { name: "Sign up" })
    expect(signup).toHaveAttribute("href", "/signup")
    await userEvent.click(signup)

    expect(window.location.pathname).toBe("/signup")
    expect(
      screen.getByRole("heading", { name: "Create your Trellis account" })
    ).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/login"
    )

    act(() => window.history.back())
    await screen.findByRole("heading", { name: "Sign in to Trellis" })
    expect(window.location.pathname).toBe("/login")

    act(() => window.history.forward())
    await screen.findByRole("heading", { name: "Create your Trellis account" })
    expect(window.location.pathname).toBe("/signup")

    await userEvent.click(screen.getByRole("link", { name: "Sign in" }))
    expect(window.location.pathname).toBe("/login")
    expect(
      screen.getByRole("heading", { name: "Sign in to Trellis" })
    ).toBeInTheDocument()
  })

  it.each(["/login", "/signup"])(
    "sends a signed-in visit to %s back to the workspace",
    async (path) => {
      renderAuth(path, session)

      expect(await screen.findByText("Workspace content")).toBeInTheDocument()
      await waitFor(() => expect(window.location.pathname).toBe("/"))
    }
  )

  it("returns to /login after the session signs out", async () => {
    const auth = renderAuth("/", session)
    await screen.findByText("Workspace content")

    act(() => auth.authChanged("SIGNED_OUT", null))

    await screen.findByRole("heading", { name: "Sign in to Trellis" })
    await waitFor(() => expect(window.location.pathname).toBe("/login"))
    expect(screen.queryByText("Workspace content")).not.toBeInTheDocument()
  })

  it.each(["/login", "/signup"])(
    "uses the existing OAuth flow from %s",
    async (path) => {
      const { sdk, navigate } = renderAuth(path)
      await screen.findByRole("button", { name: "Continue with Google" })
      await userEvent.click(
        screen.getByRole("button", { name: "Continue with Google" })
      )

      await waitFor(() =>
        expect(navigate).toHaveBeenCalledWith(
          "https://provider.example/authorize"
        )
      )
      expect(sdk.auth.signInWithOAuth).toHaveBeenCalledWith({
        provider: "google",
        options: {
          redirectTo: `${window.location.origin}/`,
          skipBrowserRedirect: true,
        },
      })
      expect(
        screen.getByRole("button", { name: "Connecting to Google…" })
      ).toBeDisabled()
      expect(
        screen.getByRole("button", { name: "Continue with GitHub" })
      ).toBeDisabled()
    }
  )

  it("exchanges a callback code before routing to the workspace", async () => {
    const { sdk } = renderAuth("/?code=test-callback&sb_flow_id=test-flow")

    await screen.findByText("Workspace content")
    expect(sdk.auth.exchangeCodeForSession).toHaveBeenCalledWith(
      "test-callback",
      { flowId: "test-flow" }
    )
    expect(window.location.pathname).toBe("/")
    expect(window.location.search).toBe("")
  })

  it("preserves a denied OAuth callback error when routing to /login", async () => {
    renderAuth("/?error=access_denied&error_description=Cancelled")

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Sign-in was cancelled or denied"
    )
    await waitFor(() => expect(window.location.pathname).toBe("/login"))
    expect(window.location.search).toBe("")
  })
})
