import { expect, test } from "bun:test"
import { ApiError } from "../apps/web/src/lib/api.ts"

async function helpers() {
  return import("../apps/web/src/lib/query-client.ts")
}

test("every signed-in session receives a separate private cache", async () => {
  const { createSessionQueryClient, disposeSessionQueryClient } =
    await helpers()
  const first = createSessionQueryClient()
  const second = createSessionQueryClient()
  first.setQueryData(["settings"], { selected_model_id: "account-a-model" })
  expect(second.getQueryData(["settings"])).toBeUndefined()
  disposeSessionQueryClient(first)
  expect(first.getQueryCache().getAll()).toHaveLength(0)
  expect(second.getQueryData(["settings"])).toBeUndefined()
  disposeSessionQueryClient(second)
})

test("session cleanup aborts an in-flight query and rejects its late private result", async () => {
  const { createSessionQueryClient, disposeSessionQueryClient } =
    await helpers()
  const client = createSessionQueryClient()
  let receivedSignal
  let release
  const query = client
    .fetchQuery({
      queryKey: ["chat", "account-a-chat"],
      queryFn: ({ signal }) => {
        receivedSignal = signal
        return new Promise((resolve) => (release = resolve))
      },
    })
    .catch((error) => error)
  disposeSessionQueryClient(client)
  expect(receivedSignal.aborted).toBe(true)
  release({ content: "Private answer" })
  await query
  expect(client.getQueryCache().getAll()).toHaveLength(0)
})

test("authentication and ordinary client errors never retry, while outages have one bounded retry", async () => {
  const { createSessionQueryClient, disposeSessionQueryClient } =
    await helpers()
  const client = createSessionQueryClient()
  const retry = client.getDefaultOptions().queries.retry
  expect(
    retry(0, new ApiError("authentication_required", "Sign in", 401))
  ).toBe(false)
  expect(retry(0, new ApiError("auth_unavailable", "Unavailable", 503))).toBe(
    false
  )
  expect(retry(0, new ApiError("request_failed", "Invalid", 400))).toBe(false)
  expect(retry(0, new DOMException("Cancelled", "AbortError"))).toBe(false)
  expect(retry(0, new ApiError("service_unavailable", "Offline", 0))).toBe(true)
  expect(retry(1, new ApiError("service_unavailable", "Offline", 0))).toBe(
    false
  )
  expect(client.getDefaultOptions().queries.gcTime).toBe(300000)
  expect(client.getDefaultOptions().queries.refetchOnWindowFocus).toBe(false)
  disposeSessionQueryClient(client)
})

test("non-idempotent mutations run once and are removed at session cleanup", async () => {
  const { createSessionQueryClient, disposeSessionQueryClient } =
    await helpers()
  const client = createSessionQueryClient()
  let calls = 0
  const mutation = client.getMutationCache().build(client, {
    mutationFn: async () => {
      calls += 1
      throw new ApiError("service_unavailable", "Offline", 0)
    },
  })
  await expect(mutation.execute(undefined)).rejects.toMatchObject({
    code: "service_unavailable",
  })
  expect(calls).toBe(1)
  expect(client.getDefaultOptions().mutations.retry).toBe(false)
  expect(client.getDefaultOptions().mutations.networkMode).toBe("always")
  disposeSessionQueryClient(client)
  expect(client.getMutationCache().getAll()).toHaveLength(0)
})
