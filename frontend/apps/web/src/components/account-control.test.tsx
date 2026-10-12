import type { Session, SupabaseClient, User } from "@supabase/supabase-js"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AccountControl } from "@/components/account-control"
import {
  createAuthController,
  type AuthController,
} from "@/lib/auth-controller"
import type { Profile } from "@/lib/app-types"

const currentAuth = vi.hoisted(() => ({
  controller: null as AuthController | null,
}))

vi.mock("@/lib/auth-controller", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/auth-controller")>()
  return { ...actual, getAuthController: () => currentAuth.controller! }
})

const user: User = {
  id: "account-1",
  aud: "authenticated",
  email: "ada@example.com",
  app_metadata: { provider: "google" },
  user_metadata: { full_name: "OAuth name" },
  created_at: "2026-10-10T00:00:00Z",
}

const profile: Profile = {
  id: "account-1",
  display_name: "Ada Lovelace",
  email: "ada@example.com",
  created_at: "2026-10-10T00:00:00Z",
  updated_at: "2026-10-10T00:00:00Z",
}

function renderAccount(savedProfile?: Profile | null) {
  const session: Session = {
    access_token: "test-access-token",
    refresh_token: "test-refresh-token",
    expires_in: 3600,
    token_type: "bearer",
    user,
  }
  const sdk = {
    auth: {
      getSession: async () => ({ data: { session }, error: null }),
      getUser: async () => ({ data: { user }, error: null }),
      onAuthStateChange: () => ({
        data: { subscription: { unsubscribe: () => {} } },
      }),
    },
  }
  currentAuth.controller = createAuthController(
    sdk as unknown as SupabaseClient
  )
  return render(<AccountControl profile={savedProfile} />)
}

afterEach(cleanup)

describe("account display name", () => {
  it("uses the persisted profile name instead of OAuth metadata", async () => {
    renderAccount(profile)

    expect(
      await screen.findByLabelText("Account: Ada Lovelace")
    ).toBeInTheDocument()
    expect(
      screen.queryByLabelText("Account: OAuth name")
    ).not.toBeInTheDocument()
  })

  it("uses OAuth metadata while the saved profile is unavailable", async () => {
    renderAccount(null)

    expect(
      await screen.findByLabelText("Account: OAuth name")
    ).toBeInTheDocument()
  })

  it("falls back to OAuth metadata when the saved name is blank", async () => {
    renderAccount({ ...profile, display_name: "  " })

    expect(
      await screen.findByLabelText("Account: OAuth name")
    ).toBeInTheDocument()
  })

  it("updates the account label after the saved profile changes", async () => {
    const view = renderAccount(profile)
    await screen.findByLabelText("Account: Ada Lovelace")

    view.rerender(
      <AccountControl profile={{ ...profile, display_name: "Ada Byron" }} />
    )

    expect(screen.getByLabelText("Account: Ada Byron")).toBeInTheDocument()
    expect(
      screen.queryByLabelText("Account: Ada Lovelace")
    ).not.toBeInTheDocument()
  })
})
