import { expect, test } from "bun:test"
import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "../apps/web/src/lib/query-client.ts"

const input = {
  session_id: "chat-a",
  model_id: "model-a",
  draft: "Unchanged draft",
  budget_preset: "conservative",
}
const response = {
  model_id: "model-a",
  context_window_tokens: 100000,
  input_tokens: 2000,
  count_method: "utf8_bytes_estimate_v1",
  output_reserve_tokens: 4096,
  safety_margin_tokens: 2048,
  usable_input_tokens: 93856,
  compaction_threshold_tokens: 75084,
  percentage: 2,
  status: "ready",
}

test("preview query identity includes chat, model, draft, budget and run generation with no retained inactive drafts", async () => {
  const { contextPreviewQueryOptions } =
    await import("../apps/web/src/lib/server-queries.ts")
  const options = contextPreviewQueryOptions(input, 3)
  for (const changed of [
    { ...input, session_id: "chat-b" },
    { ...input, model_id: "model-b" },
    { ...input, draft: "Different" },
    { ...input, budget_preset: "longer" },
  ])
    expect(contextPreviewQueryOptions(changed, 3).queryKey).not.toEqual(
      options.queryKey
    )
  expect(contextPreviewQueryOptions(input, 4).queryKey).not.toEqual(
    options.queryKey
  )
  expect(options.gcTime).toBe(0)
  expect(options.retry).toBe(false)
})

test("a superseded preview is aborted and cannot replace the latest model's result", async () => {
  const { contextPreviewQueryOptions } =
    await import("../apps/web/src/lib/server-queries.ts")
  const client = createSessionQueryClient()
  let oldSignal
  let release
  const service = {
    previewContext: (request, { signal }) => {
      if (request.model_id !== "model-a")
        return Promise.resolve({ ...response, model_id: request.model_id })
      oldSignal = signal
      return new Promise((resolve) => (release = resolve))
    },
  }
  const oldOptions = contextPreviewQueryOptions(input, 3, service)
  const old = client.fetchQuery(oldOptions).catch((error) => error)
  await client.cancelQueries({ queryKey: oldOptions.queryKey })
  const currentOptions = contextPreviewQueryOptions(
    { ...input, model_id: "model-b" },
    4,
    service
  )
  const current = await client.fetchQuery(currentOptions)
  release(response)
  await old
  expect(oldSignal.aborted).toBe(true)
  expect(current.model_id).toBe("model-b")
  expect(client.getQueryData(currentOptions.queryKey).model_id).toBe("model-b")
  expect(client.getQueryData(oldOptions.queryKey)).toBeUndefined()
  disposeSessionQueryClient(client)
})

test("oversized previews fail clearly and never transmit a truncated draft", async () => {
  const { contextPreviewQueryOptions } =
    await import("../apps/web/src/lib/server-queries.ts")
  const client = createSessionQueryClient()
  let calls = 0
  const options = contextPreviewQueryOptions(
    { ...input, draft: "a".repeat(32001) },
    1,
    {
      previewContext: async () => {
        calls += 1
        return response
      },
    }
  )
  await expect(client.fetchQuery(options)).rejects.toMatchObject({
    code: "context_preview_draft_too_long",
    status: 400,
  })
  expect(calls).toBe(0)
  disposeSessionQueryClient(client)
})
