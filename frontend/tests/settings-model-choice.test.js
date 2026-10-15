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
  expect(markup).toContain("Metadata pending review")
  expect(markup).toContain('value="pending" disabled="" selected=""')
  expect(markup).not.toContain("will power new messages")
})

test("Settings uses one compact grouped model select and keeps unavailable saved model names visible", async () => {
  const { SettingsPage } =
    await import("../apps/web/src/components/settings-page.tsx")
  const markup = renderToStaticMarkup(
    createElement(SettingsPage, {
      profile: { id: "account", display_name: "Name", email: null },
      settings: {
        selected_model_id: "anthropic:sonnet",
        providers: [],
        models: [
          {
            id: "openai:gpt",
            name: "GPT ready",
            provider_id: "openai",
            provider_name: "OpenAI",
            selectable: true,
          },
          ...["Sonnet", "Opus"].map((name) => ({
            id: `anthropic:${name.toLowerCase()}`,
            name: `Claude ${name}`,
            provider_id: "anthropic",
            provider_name: "Anthropic",
            requires_api_key: true,
            configured: false,
          })),
        ],
      },
      onProfileChange: () => {},
      onSettingsChange: () => {},
    })
  )
  expect(
    markup.match(/aria-label="Default model for new chats"/g)
  ).toHaveLength(1)
  expect(markup).toContain('<select aria-label="Default model for new chats"')
  expect(markup.match(/<optgroup label="Anthropic"/g)).toHaveLength(1)
  expect(markup.match(/<optgroup label="OpenAI"/g)).toHaveLength(1)
  expect(markup).toContain(
    'value="anthropic:sonnet" disabled="" selected="">Claude Sonnet · API key missing</option>'
  )
  expect(markup).toContain("Claude Opus · API key missing")
  expect(markup).not.toContain('role="radio"')
  expect(markup).not.toContain('class="provider-model"')
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
