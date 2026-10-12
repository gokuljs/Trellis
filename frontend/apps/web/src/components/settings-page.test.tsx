import { cleanup, render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it } from "vitest"

import { SettingsPage } from "@/components/settings-page"
import type { OnboardingState, Profile, Settings } from "@/lib/app-types"

const profile: Profile = {
  id: "account-1",
  display_name: "Ada Lovelace",
  email: "ada@example.com",
  created_at: "2026-10-10T10:00:00Z",
  updated_at: "2026-10-10T10:05:00Z",
}

const settings: Settings = {
  selected_provider: "openai",
  selected_model_id: "openai:gpt-5.5",
  providers: [
    {
      id: "openai",
      name: "OpenAI",
      model: "GPT 5.5",
      configured: true,
      key_hint: "••••1234",
    },
  ],
  models: [
    {
      id: "openai:gpt-5.5",
      provider_id: "openai",
      provider_name: "OpenAI",
      adapter_kind: "openai",
      upstream_model_id: "gpt-5.5",
      name: "GPT 5.5",
      requires_api_key: true,
      supports_streaming: true,
      supports_tools: true,
      configured: true,
      key_hint: "••••1234",
    },
  ],
  default_budget_preset: "conservative",
}

const completed: OnboardingState = {
  current_step: "complete",
  completed: true,
}

function page(nextSettings = settings, onboarding = completed) {
  return (
    <SettingsPage
      profile={profile}
      settings={nextSettings}
      onboarding={onboarding}
      onProfileChange={() => {}}
      onSettingsChange={() => {}}
    />
  )
}

afterEach(cleanup)

describe("saved profile setup", () => {
  it("shows persisted onboarding answers and only the masked local key status", () => {
    render(page())

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Ada Lovelace")).toBeInTheDocument()
    expect(summary.getByText("ada@example.com")).toBeInTheDocument()
    expect(summary.getByText("Complete")).toBeInTheDocument()
    expect(summary.getByText("OpenAI · GPT 5.5")).toBeInTheDocument()
    expect(
      summary.getByText("Configured on this computer · ••••1234")
    ).toBeInTheDocument()
    expect(screen.getByLabelText("OpenAI API key")).toHaveValue("")
  })

  it("keeps the saved summary separate from an unsaved name draft", async () => {
    render(page())
    const user = userEvent.setup()

    await user.clear(screen.getByRole("textbox", { name: "Display name" }))
    await user.type(
      screen.getByRole("textbox", { name: "Display name" }),
      "New draft"
    )

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Ada Lovelace")).toBeInTheDocument()
    expect(summary.queryByText("New draft")).not.toBeInTheDocument()
  })

  it("keeps completed account setup distinct from missing keys on this computer", () => {
    const localSettings: Settings = {
      ...settings,
      providers: settings.providers.map((provider) => ({
        ...provider,
        configured: false,
        key_hint: null,
      })),
      models: settings.models?.map((model) => ({
        ...model,
        configured: false,
        key_hint: null,
      })),
    }
    render(page(localSettings))

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Complete")).toBeInTheDocument()
    expect(
      summary.getByText("Not configured on this computer")
    ).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: "Save OpenAI key" })
    ).toBeDisabled()
  })

  it("keeps the profile editable when the selected model is unavailable", () => {
    render(page({ ...settings, selected_model_id: "retired:model" }))

    expect(
      screen.getByRole("heading", { name: "Your profile" })
    ).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue(
      "Ada Lovelace"
    )
    expect(screen.getByRole("button", { name: "Save profile" })).toBeEnabled()
    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Selected model unavailable")).toBeInTheDocument()
    expect(screen.getByRole("radio", { name: /OpenAI/ })).toBeInTheDocument()
  })

  it("shows unfinished setup without claiming completion", () => {
    render(page(settings, { current_step: "model", completed: false }))

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("In progress")).toBeInTheDocument()
    expect(summary.queryByText("Complete")).not.toBeInTheDocument()
  })

  it("reports unavailable setup status when no onboarding state was supplied", () => {
    render(
      <SettingsPage
        profile={profile}
        settings={settings}
        onProfileChange={() => {}}
        onSettingsChange={() => {}}
      />
    )

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Setup status unavailable")).toBeInTheDocument()
    expect(summary.queryByText("Complete")).not.toBeInTheDocument()
  })

  it("does not request a local key for a model that needs no credentials", () => {
    const localSettings: Settings = {
      ...settings,
      models: settings.models?.map((model) => ({
        ...model,
        requires_api_key: false,
        configured: false,
        key_hint: null,
      })),
    }
    render(page(localSettings))

    const summary = within(screen.getByRole("region", { name: "Saved setup" }))
    expect(summary.getByText("Not required")).toBeInTheDocument()
    expect(screen.queryByLabelText("OpenAI API key")).not.toBeInTheDocument()
  })
})
