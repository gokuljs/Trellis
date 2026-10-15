import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

async function renderChoice(overrides = {}) {
  const { ModelSelect } =
    await import("../apps/web/src/components/model-select.tsx")
  return renderToStaticMarkup(
    createElement(ModelSelect, {
      models: [
        { id: "ready", name: "Ready model", selectable: true },
        { id: "pending", name: "Pending model", metadata_status: "pending" },
        { id: "unavailable", name: "Unavailable model", selectable: false },
      ],
      selectedModelId: "ready",
      disabled: false,
      saving: false,
      onChange: () => {},
      ...overrides,
    })
  )
}

test("the composer model selector exposes only names and enables every catalog choice", async () => {
  const markup = await renderChoice()
  expect(markup).toContain('class="composer-select composer-model-select"')
  expect(markup).toContain('aria-label="Chat model"')
  expect(markup).toContain('value="ready" selected=""')
  expect(markup).toContain('value="pending">Pending model</option>')
  expect(markup).toContain('value="unavailable">Unavailable model</option>')
  expect(markup).not.toContain("Metadata pending review")
  expect(markup).not.toContain("<optgroup")
})

test("the native chat selector leaves API-key and unchecked availability errors to the request", async () => {
  const markup = await renderChoice({
    models: [
      {
        id: "openai:ready",
        name: "GPT ready",
        provider_id: "openai",
        provider_name: "OpenAI",
        selectable: true,
      },
      {
        id: "anthropic:sonnet",
        name: "Claude Sonnet",
        provider_id: "anthropic",
        provider_name: "Anthropic",
        requires_api_key: true,
        configured: false,
      },
      {
        id: "openai:unchecked",
        name: "GPT unchecked",
        provider_id: "openai",
        provider_name: "OpenAI",
        enabled: false,
        availability: "unknown",
      },
    ],
    selectedModelId: "openai:ready",
  })
  expect(markup).not.toContain("<optgroup")
  expect(markup).toContain(
    'value="openai:ready" selected="">GPT ready</option>'
  )
  expect(markup).toContain('value="anthropic:sonnet">Claude Sonnet</option>')
  expect(markup).toContain('value="openai:unchecked">GPT unchecked</option>')
})

test("an unknown saved choice remains visible as its ID until a catalog model is chosen", async () => {
  const markup = await renderChoice({ selectedModelId: "unknown" })
  expect(markup).toContain('value="unknown" disabled="" selected=""')
  expect(markup).toContain(
    'value="unknown" disabled="" selected="">unknown</option>'
  )
  expect(markup).not.toContain("Saved model unavailable")
})

test("model changes are disabled during a run or save and saving has status text", async () => {
  const active = await renderChoice({ disabled: true })
  expect(active).toContain('aria-label="Chat model" disabled=""')
  const saving = await renderChoice({ saving: true })
  expect(saving).toContain('aria-busy="true"')
  expect(saving).toContain('role="status"')
  expect(saving).toContain("Saving chat model…")
})

test("the real composer lets a known model send even when its availability metadata is pending", async () => {
  const { Composer } = await import("../apps/web/src/components/composer.tsx")
  const { ThemeProvider } =
    await import("../apps/web/src/components/theme-provider.tsx")
  const originalStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => "dark" }
  try {
    const markup = renderToStaticMarkup(
      createElement(
        ThemeProvider,
        { defaultTheme: "dark" },
        createElement(Composer, {
          value: "Question",
          placeholder: "Message",
          onChange: () => {},
          onSubmit: () => {},
          budgetPreset: "conservative",
          onBudgetChange: () => {},
          models: [
            {
              id: "pending",
              name: "Pending model",
              metadata_status: "pending",
            },
          ],
          selectedModelId: "pending",
          modelSaving: false,
          onModelChange: () => {},
        })
      )
    )
    expect(markup).toContain('aria-label="Chat model"')
    expect(markup).toContain('aria-label="Run budget"')
    expect(markup).toContain('aria-label="Send"')
    expect(markup).not.toContain('aria-label="Send" disabled=""')
    expect(markup).not.toContain('aria-label="Message" disabled=""')
  } finally {
    if (originalStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = originalStorage
  }
})
