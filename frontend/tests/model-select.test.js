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

test("the composer model selector exposes names and disables pending choices", async () => {
  const markup = await renderChoice()
  expect(markup).toContain('class="composer-select"')
  expect(markup).toContain('aria-label="Chat model"')
  expect(markup).toContain('value="ready" selected=""')
  expect(markup).toContain('value="pending" disabled=""')
  expect(markup).toContain("Pending model · Pending")
  expect(markup).toContain('value="unavailable" disabled=""')
})

test("an unknown saved choice remains visible until a ready model is chosen", async () => {
  const markup = await renderChoice({ selectedModelId: "unknown" })
  expect(markup).toContain('value="unknown" disabled="" selected=""')
  expect(markup).toContain("Saved model unavailable")
})

test("model changes are disabled during a run or save and saving has status text", async () => {
  const active = await renderChoice({ disabled: true })
  expect(active).toContain('aria-label="Chat model" disabled=""')
  const saving = await renderChoice({ saving: true })
  expect(saving).toContain('aria-busy="true"')
  expect(saving).toContain('role="status"')
  expect(saving).toContain("Saving chat model…")
})

test("the real composer includes the chat picker and keeps Send disabled for an unavailable choice", async () => {
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
          modelReady: false,
          onModelChange: () => {},
        })
      )
    )
    expect(markup).toContain('aria-label="Chat model"')
    expect(markup).toContain('aria-label="Run budget"')
    expect(markup).toContain('aria-label="Send" disabled=""')
    expect(markup).not.toContain('aria-label="Message" disabled=""')
  } finally {
    if (originalStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = originalStorage
  }
})
