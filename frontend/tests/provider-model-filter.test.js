import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { ModelSelect } from "../apps/web/src/components/model-select.tsx"
import { SettingsPage } from "../apps/web/src/components/settings-page.tsx"
import { createApiClient } from "../apps/web/src/lib/api.ts"

const models = [
  {
    id: "openai:gpt",
    name: "GPT",
    provider_id: "openai",
    provider_name: "OpenAI",
  },
  {
    id: "openai:luna",
    name: "GPT Luna",
    provider_id: "openai",
    provider_name: "OpenAI",
    enabled: false,
    availability: "unknown",
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
    id: "anthropic:opus",
    name: "Claude Opus",
    provider_id: "anthropic",
    provider_name: "Anthropic",
  },
]
const selectMarkup = (markup, label) =>
  markup.match(
    new RegExp(`<select[^>]*aria-label="${label}"[^>]*>([\\s\\S]*?)<\\/select>`)
  )?.[1] ?? ""
const chatProps = (selectedModelId, changes = {}) => ({
  models,
  selectedModelId,
  disabled: false,
  saving: false,
  onChange: () => {},
  ...changes,
})

test.each([
  ["openai:gpt", ["GPT", "GPT Luna"], ["Claude Sonnet", "Claude Opus"]],
  ["anthropic:sonnet", ["Claude Sonnet", "Claude Opus"], ["GPT Luna", ">GPT<"]],
])(
  "chat model options follow the selected model's provider: %s",
  (selected, included, excluded) => {
    const markup = selectMarkup(
      renderToStaticMarkup(createElement(ModelSelect, chatProps(selected))),
      "Chat model"
    )
    for (const name of included) expect(markup).toContain(name)
    for (const name of excluded) expect(markup).not.toContain(name)
    expect(markup).not.toContain("disabled")
    expect(markup).not.toContain("optgroup")
    expect(markup).not.toContain("API key")
  }
)

test("a chat provider change saves its first model through the existing chat model callback", () => {
  const changes = []
  const tree = ModelSelect(
    chatProps("openai:gpt", { onChange: (id) => changes.push(id) })
  )
  const provider = tree.props.children.find(
    (child) => child?.props?.["aria-label"] === "Chat provider"
  )
  expect(provider).toBeDefined()
  provider.props.onChange({ target: { value: "anthropic" } })
  expect(changes).toEqual(["anthropic:sonnet"])
  const markup = renderToStaticMarkup(tree)
  expect(selectMarkup(markup, "Chat provider")).toContain(
    'value="openai" selected=""'
  )
  expect(selectMarkup(markup, "Chat provider")).toContain('value="anthropic"')
})

test.each([{ disabled: true }, { saving: true }])(
  "both chat selectors remain locked during a run or save: %j",
  (changes) => {
    const markup = renderToStaticMarkup(
      createElement(ModelSelect, chatProps("openai:gpt", changes))
    )
    expect(markup).toMatch(/aria-label="Chat provider" disabled=""/)
    expect(markup).toMatch(/aria-label="Chat model" disabled=""/)
  }
)

test("unknown saved IDs infer only recognized providers and never reveal cross-provider model choices", () => {
  const knownPrefix = selectMarkup(
    renderToStaticMarkup(createElement(ModelSelect, chatProps("openai:old"))),
    "Chat model"
  )
  expect(knownPrefix).toContain(
    'value="openai:old" disabled="" selected="">openai:old</option>'
  )
  expect(knownPrefix).toContain("GPT Luna")
  expect(knownPrefix).not.toContain("Claude")
  const unknown = selectMarkup(
    renderToStaticMarkup(createElement(ModelSelect, chatProps("unrecognized"))),
    "Chat model"
  )
  expect(unknown).toContain(">unrecognized</option>")
  expect(unknown).not.toContain("GPT")
  expect(unknown).not.toContain("Claude")
})

test.each(["unrecognized", "anthropic:missing"])(
  "an unknown saved chat can switch to a singleton catalog provider: %s",
  (selected) => {
    const changes = []
    const tree = ModelSelect(
      chatProps(selected, {
        models: models.slice(0, 2),
        onChange: (id) => changes.push(id),
      })
    )
    const provider = tree.props.children.find(
      (child) => child?.props?.["aria-label"] === "Chat provider"
    )
    expect(provider).toBeDefined()
    const markup = renderToStaticMarkup(tree)
    expect(selectMarkup(markup, "Chat model")).toContain(
      `>${selected}</option>`
    )
    expect(selectMarkup(markup, "Chat provider")).toContain('value="openai"')
    provider.props.onChange({ target: { value: "openai" } })
    expect(changes).toEqual(["openai:gpt"])
  }
)

test("a saved chat keeps its model provider when the new-chat default changes", async () => {
  const { selectedChatModelId } =
    await import("../apps/web/src/lib/model-choice.ts")
  const selectedModelId = selectedChatModelId(
    { model_id: "anthropic:opus" },
    { selected_model_id: "openai:gpt", models }
  )
  const markup = selectMarkup(
    renderToStaticMarkup(
      createElement(ModelSelect, chatProps(selectedModelId))
    ),
    "Chat model"
  )
  expect(markup).toContain("Claude Sonnet")
  expect(markup).toContain('value="anthropic:opus" selected=""')
  expect(markup).not.toContain("GPT")
})

test("Settings offers a provider switch and shows only that provider's model names after the saved response", () => {
  const render = (provider, modelId) =>
    renderToStaticMarkup(
      createElement(SettingsPage, {
        profile: { id: "account", display_name: "Name", email: null },
        settings: {
          selected_provider: provider,
          selected_model_id: modelId,
          providers: [
            { id: "openai", name: "OpenAI" },
            { id: "anthropic", name: "Anthropic" },
          ],
          models,
        },
        onProfileChange: () => {},
        onSettingsChange: () => {},
      })
    )
  const openai = render("openai", "openai:gpt")
  expect(selectMarkup(openai, "Default provider for new chats")).toContain(
    'value="openai" selected=""'
  )
  expect(selectMarkup(openai, "Default provider for new chats")).toContain(
    'value="anthropic"'
  )
  expect(selectMarkup(openai, "Default model for new chats")).toContain(
    "GPT Luna"
  )
  expect(selectMarkup(openai, "Default model for new chats")).not.toContain(
    "Claude"
  )
  const anthropic = render("anthropic", "anthropic:sonnet")
  expect(selectMarkup(anthropic, "Default provider for new chats")).toContain(
    'value="anthropic" selected=""'
  )
  expect(selectMarkup(anthropic, "Default model for new chats")).toContain(
    "Claude Opus"
  )
  expect(selectMarkup(anthropic, "Default model for new chats")).not.toContain(
    "GPT"
  )
  expect(anthropic).not.toContain("API key")
})

test("the provider client uses the existing settings endpoint and returns its model choice", async () => {
  const credential = { userId: "account", sessionEpoch: 1, token: "fake" }
  const calls = []
  const client = createApiClient({
    getAuthController: () => ({
      getSnapshot: () => ({
        status: "signed-in",
        user: { id: "account" },
        sessionEpoch: 1,
      }),
      getAccessToken: async () => credential,
      isCredentialCurrent: () => true,
    }),
    fetch: async (url, init) => {
      calls.push({ url, method: init.method, body: JSON.parse(init.body) })
      return Response.json({
        selected_provider: "anthropic",
        selected_model_id: "anthropic:sonnet",
      })
    },
  })
  expect(typeof client.selectProvider).toBe("function")
  const result = await client.selectProvider("anthropic")
  expect(calls).toEqual([
    {
      url: "/api/settings/provider",
      method: "PUT",
      body: { provider: "anthropic" },
    },
  ])
  expect(result.selected_model_id).toBe("anthropic:sonnet")
})
