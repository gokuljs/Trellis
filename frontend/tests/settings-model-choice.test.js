import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

test("Settings describes the model as a new-chat default and never offers pending metadata as ready", async () => {
  const { SettingsPage } =
    await import("../apps/web/src/components/settings-page.tsx")
  const markup = renderToStaticMarkup(
    createElement(SettingsPage, {
      profile: {
        id: "account",
        display_name: "Name",
        email: "name@example.test",
      },
      settings: {
        selected_model_id: "pending",
        providers: [],
        models: [
          {
            id: "pending",
            name: "Pending model",
            provider_name: "Provider",
            metadata_status: "pending",
            configured: true,
          },
        ],
      },
      onProfileChange: () => {},
      onSettingsChange: () => {},
    })
  )
  expect(markup).toContain("Default model")
  expect(markup).toContain("Existing chats keep their own choice.")
  expect(markup).toContain("Pending")
  expect(markup).toContain('aria-checked="true" disabled=""')
  expect(markup).not.toContain("will power new messages")
})

test("onboarding keeps pending models disabled and gives a ready model the keyboard entry point", async () => {
  const { OnboardingFlow } =
    await import("../apps/web/src/components/onboarding-flow.tsx")
  const markup = renderToStaticMarkup(
    createElement(OnboardingFlow, {
      profile: { id: "account", display_name: "Name" },
      settings: {
        selected_model_id: "pending",
        providers: [],
        models: [
          {
            id: "pending",
            name: "Pending model",
            metadata_status: "pending",
            configured: true,
          },
          { id: "ready", name: "Ready model", selectable: true },
        ],
      },
      initialStep: "model",
      onAdvanceIntro: async () => {},
      onSaveProfile: async () => {},
      onRetryModels: async () => {},
      onComplete: async () => {},
    })
  )
  expect(markup).toContain('aria-checked="true" tabindex="-1" disabled=""')
  expect(markup).toContain('aria-checked="false" tabindex="0"')
  expect(markup).toContain("Pending")
})
