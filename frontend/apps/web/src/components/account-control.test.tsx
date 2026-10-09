import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import type { User } from "@supabase/supabase-js"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { AccountControl } from "@/components/account-control"
import type { AuthState } from "@/lib/auth-controller"

const auth = vi.hoisted(() => ({
  state: {
    status: "signed-in",
    user: null,
    error: null,
    pendingProvider: null,
  } as AuthState,
  listeners: new Set<() => void>(),
  signOut: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
}))

vi.mock("@/lib/auth-controller", () => ({
  getAuthController: () => ({
    getSnapshot: () => auth.state,
    subscribe: (listener: () => void) => {
      auth.listeners.add(listener)
      return () => auth.listeners.delete(listener)
    },
    signIn: vi.fn(),
    retry: vi.fn(),
    signOut: auth.signOut,
  }),
}))

const account: User = {
  id: "account-1",
  aud: "authenticated",
  role: "authenticated",
  email: "ada@example.com",
  app_metadata: { provider: "google" },
  user_metadata: { full_name: "Ada Lovelace" },
  created_at: "2026-10-09T00:00:00Z",
}

beforeEach(() => {
  auth.state = {
    status: "signed-in",
    user: account,
    error: null,
    pendingProvider: null,
  }
  auth.signOut.mockReset().mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  auth.listeners.clear()
})

describe("account control", () => {
  it("shows the verified account separately from local profile settings", async () => {
    const user = userEvent.setup()
    render(<AccountControl />)
    await user.click(screen.getByLabelText("Account: Ada Lovelace"))

    expect(screen.getByText("ada@example.com")).toBeVisible()
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible()
  })

  it("uses a usable identity when a provider has no name or email", async () => {
    const user = userEvent.setup()
    auth.state = {
      ...auth.state,
      user: {
        ...account,
        email: undefined,
        user_metadata: { full_name: { invalid: true } },
        app_metadata: { provider: "github" },
      },
    }
    render(<AccountControl />)
    await user.click(screen.getByLabelText("Account: GitHub account"))

    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible()
  })

  it("prevents duplicate sign-out while the request is pending", async () => {
    const user = userEvent.setup()
    let resolve!: () => void
    auth.signOut.mockImplementation(
      () =>
        new Promise<void>((done) => {
          resolve = done
        })
    )
    render(<AccountControl />)
    await user.click(screen.getByLabelText("Account: Ada Lovelace"))
    await user.click(screen.getByRole("button", { name: "Sign out" }))

    const pending = screen.getByRole("button", { name: "Signing out…" })
    expect(pending).toBeDisabled()
    await user.click(pending)
    expect(auth.signOut).toHaveBeenCalledOnce()
    await act(async () => resolve())
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled()
    )
  })

  it("reports a failed sign-out and leaves a retry action available", async () => {
    const user = userEvent.setup()
    auth.signOut.mockImplementation(async () => {
      auth.state = {
        ...auth.state,
        error: "Sign-out could not be completed. Please try again.",
      }
      for (const listener of auth.listeners) listener()
    })
    render(<AccountControl />)
    await user.click(screen.getByLabelText("Account: Ada Lovelace"))
    await user.click(screen.getByRole("button", { name: "Sign out" }))

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Sign-out could not be completed"
    )
    expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled()
  })

  it("closes the account panel with Escape and returns focus to its control", async () => {
    const user = userEvent.setup()
    render(<AccountControl />)
    const control = screen.getByLabelText("Account: Ada Lovelace")
    await user.click(control)
    screen.getByRole("button", { name: "Sign out" }).focus()
    await user.keyboard("{Escape}")

    expect(screen.getByRole("button", { name: "Sign out" })).not.toBeVisible()
    expect(control).toHaveFocus()
  })

  it("closes the account panel when clicking elsewhere", async () => {
    const user = userEvent.setup()
    render(
      <>
        <AccountControl />
        <button>Outside</button>
      </>
    )
    await user.click(screen.getByLabelText("Account: Ada Lovelace"))
    await user.click(screen.getByRole("button", { name: "Outside" }))

    expect(screen.getByRole("button", { name: "Sign out" })).not.toBeVisible()
  })
})
