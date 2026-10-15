import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

const preview = {
  model_id: "model-a",
  context_window_tokens: 100000,
  input_tokens: 25000,
  count_method: "utf8_bytes_estimate_v1",
  output_reserve_tokens: 4096,
  safety_margin_tokens: 2048,
  usable_input_tokens: 93856,
  compaction_threshold_tokens: 75084,
  percentage: 25,
  status: "ready",
}

test("the idle gauge is a small accessible button with no visible token numbers", async () => {
  const { ContextGauge } =
    await import("../apps/web/src/components/context-gauge.tsx")
  const markup = renderToStaticMarkup(
    createElement(ContextGauge, {
      preview,
      modelName: "Model A",
      loading: false,
      error: null,
    })
  )
  expect(markup).toContain('aria-label="Show context usage"')
  expect(markup).toContain('aria-expanded="false"')
  expect(markup).toContain('class="context-gauge-ring"')
  expect(markup).not.toContain("25,000")
  expect(markup).not.toContain("100,000")
  expect(markup).not.toContain("<svg")
})

test("gauge details display active-context estimates, output reservation and compaction status", async () => {
  const { ContextGaugeDetails } =
    await import("../apps/web/src/components/context-gauge.tsx")
  const markup = renderToStaticMarkup(
    createElement(ContextGaugeDetails, {
      preview,
      modelName: "Model A",
      loading: false,
      error: null,
    })
  )
  expect(markup).toContain("~25,000 / 100,000 tokens")
  expect(markup).toContain("~25% of model context")
  expect(markup).toContain("4,096 tokens")
  expect(markup).toContain("75,084 tokens")
  const saving = renderToStaticMarkup(
    createElement(ContextGaugeDetails, {
      preview,
      modelName: "Model A",
      loading: false,
      error: null,
      compacting: true,
    })
  )
  expect(saving).toContain("Compacting and saving context…")
  expect(saving).not.toContain("Compacted context saved.")
  const saved = renderToStaticMarkup(
    createElement(ContextGaugeDetails, {
      preview: { ...preview, checkpoint_id: "checkpoint" },
      modelName: "Model A",
      loading: false,
      error: null,
    })
  )
  expect(saved).toContain("Compacted context saved.")
})

test("preview failures and oversized drafts remain local to the gauge details", async () => {
  const { ContextGaugeDetails } =
    await import("../apps/web/src/components/context-gauge.tsx")
  const markup = renderToStaticMarkup(
    createElement(ContextGaugeDetails, {
      preview: null,
      modelName: "Model A",
      loading: false,
      error: "Context preview supports drafts up to 32,000 characters.",
    })
  )
  expect(markup).toContain(
    "Context preview supports drafts up to 32,000 characters."
  )
  expect(markup).not.toContain("Active context")
  expect(markup).not.toContain('role="alert"')
})
