import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Profile, Settings } from "@/lib/app-types"
import { OnboardingFlow } from "@/components/onboarding-flow"

const profile: Profile = {
  id: "profile-1",
  display_name: null,
  email: null,
  created_at: "2026-08-24T10:00:00Z",
  updated_at: "2026-08-24T10:00:00Z",
}

const settings: Settings = {
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

function renderOnboarding(overrides: Partial<Settings> = {}) {
  const onComplete = vi.fn().mockResolvedValue(undefined)
  render(
    <OnboardingFlow
      profile={profile}
      settings={{ ...settings, ...overrides }}
      onComplete={onComplete}
    />
  )
  return { onComplete }
}

async function reachModelStep() {
  const user = userEvent.setup()
  await user.click(screen.getByRole("button", { name: "Start onboarding" }))
  await user.type(screen.getByRole("textbox", { name: "Name" }), "Ada")
  await user.type(
    screen.getByRole("textbox", { name: "Email" }),
    "ada@example.com"
  )
  await user.click(screen.getByRole("button", { name: "Continue" }))
  return user
}

afterEach(cleanup)

describe("OnboardingFlow", () => {
  it("starts with the dark branded intro and advances to the profile step", async () => {
    renderOnboarding()

    expect(
      screen.getByRole("heading", { name: /get started/i })
    ).toHaveAccessibleName("Get started")
    expect(screen.getByText("01 / 03")).toBeInTheDocument()

    await userEvent.click(
      screen.getByRole("button", { name: "Start onboarding" })
    )

    expect(
      await screen.findByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByText("02 / 03")).toBeInTheDocument()
  })

  it("blocks profile progression until name and a valid email are present", async () => {
    renderOnboarding()
    const user = userEvent.setup()
    await user.click(screen.getByRole("button", { name: "Start onboarding" }))
    await user.click(screen.getByRole("button", { name: "Continue" }))

    expect(
      screen.getByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByText("02 / 03")).toBeInTheDocument()

    await user.type(screen.getByRole("textbox", { name: "Name" }), "Ada")
    await user.type(
      screen.getByRole("textbox", { name: "Email" }),
      "not-an-email"
    )
    await user.click(screen.getByRole("button", { name: "Continue" }))

    expect(
      screen.getByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
  })

  it("requires an API key for an unconfigured provider", async () => {
    const { onComplete } = renderOnboarding({
      selected_provider: "anthropic",
    })
    const user = await reachModelStep()

    expect(screen.getByText("03 / 03")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Start Trellis" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add an API key for Anthropic."
    )
    expect(onComplete).not.toHaveBeenCalled()
  })

  it("accepts an existing configured key without asking for its secret again", async () => {
    const { onComplete } = renderOnboarding()
    await reachModelStep()

    await userEvent.click(screen.getByRole("button", { name: "Start Trellis" }))

    await waitFor(() =>
      expect(onComplete).toHaveBeenCalledWith({
        displayName: "Ada",
        email: "ada@example.com",
        provider: "openai",
        apiKey: "",
      })
    )
  })

  it("clears an unsaved key when the provider changes", async () => {
    renderOnboarding()
    const user = await reachModelStep()

    await user.click(screen.getByRole("radio", { name: /Anthropic/ }))
    const apiKey = screen.getByLabelText("Anthropic API key")
    await user.type(apiKey, "sk-ant-draft")

    await user.click(screen.getByRole("radio", { name: /OpenAI/ }))
    await user.click(screen.getByRole("radio", { name: /Anthropic/ }))

    expect(screen.getByLabelText("Anthropic API key")).toHaveValue("")
  })

  it("moves provider selection with arrow keys using one tab stop", async () => {
    renderOnboarding()
    const user = await reachModelStep()
    const openAi = screen.getByRole("radio", { name: /OpenAI/ })
    const anthropic = screen.getByRole("radio", { name: /Anthropic/ })

    expect(openAi).toHaveAttribute("tabindex", "0")
    expect(anthropic).toHaveAttribute("tabindex", "-1")

    openAi.focus()
    await user.keyboard("{ArrowDown}")

    expect(anthropic).toHaveFocus()
    expect(anthropic).toHaveAttribute("aria-checked", "true")
    expect(openAi).toHaveAttribute("tabindex", "-1")
    expect(anthropic).toHaveAttribute("tabindex", "0")
  })
})
