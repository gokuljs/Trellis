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
  selected_model_id: "openai:gpt-5.5",
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

function renderOnboarding(
  overrides: Partial<Settings> = {},
  initialStep: "intro" | "profile" | "model" = "intro"
) {
  const onAdvanceIntro = vi.fn().mockResolvedValue(undefined)
  const onSaveProfile = vi.fn().mockResolvedValue(undefined)
  const onComplete = vi.fn().mockResolvedValue(undefined)
  render(
    <OnboardingFlow
      profile={profile}
      settings={{ ...settings, ...overrides }}
      initialStep={initialStep}
      onAdvanceIntro={onAdvanceIntro}
      onSaveProfile={onSaveProfile}
      onComplete={onComplete}
    />
  )
  return { onAdvanceIntro, onSaveProfile, onComplete }
}

async function reachModelStep() {
  const user = userEvent.setup()
  await user.click(screen.getByRole("button", { name: "Continue" }))
  await user.type(screen.getByRole("textbox", { name: "Name" }), "Ada")
  await user.click(screen.getByRole("button", { name: "Continue" }))
  await waitFor(() =>
    expect(
      screen.getByRole("heading", { name: "Choose a model" })
    ).toHaveFocus()
  )
  return user
}

afterEach(cleanup)

describe("OnboardingFlow", () => {
  it("starts with the dark branded intro and advances to the profile step", async () => {
    renderOnboarding()

    expect(
      screen.getByRole("heading", { name: "Workspace setup" })
    ).toHaveAccessibleName("Workspace setup")
    expect(
      screen.getByText("Create your profile and connect a model.")
    ).toBeInTheDocument()
    expect(screen.getByText("01 / 03")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: "Continue" })).toHaveLength(1)
    expect(
      screen.queryByRole("button", { name: "Get started" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Start onboarding" })
    ).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Continue" }))

    expect(
      await screen.findByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByText("02 / 03")).toBeInTheDocument()
  })

  it("uses the sign-in email and saves only the display name", async () => {
    const { onSaveProfile } = renderOnboarding()
    const user = userEvent.setup()
    await user.click(screen.getByRole("button", { name: "Continue" }))
    await user.click(screen.getByRole("button", { name: "Continue" }))

    expect(
      screen.getByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByText("02 / 03")).toBeInTheDocument()

    await user.type(screen.getByRole("textbox", { name: "Name" }), "Ada")
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveAttribute(
      "readonly"
    )
    await user.click(screen.getByRole("button", { name: "Continue" }))
    await waitFor(() => expect(onSaveProfile).toHaveBeenCalledWith("Ada"))
  })

  it("requires an API key for an unconfigured model provider", async () => {
    const { onComplete } = renderOnboarding({
      selected_provider: "anthropic",
      selected_model_id: "anthropic:claude-sonnet-5",
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
        modelId: "openai:gpt-5.5",
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

  it("resumes at the server-provided profile step", async () => {
    renderOnboarding({}, "profile")

    expect(
      await screen.findByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByText("02 / 03")).toBeInTheDocument()
  })
})
