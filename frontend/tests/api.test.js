import { afterEach, expect, mock, test } from "bun:test"

const originalFetch = globalThis.fetch
const auth = {
  getAccessToken: async () => ({
    token: "test-token",
    userId: "account-a",
    sessionEpoch: 1,
  }),
  getSessionEpoch: () => 1,
  getSnapshot: () => ({ status: "signed-in", user: { id: "account-a" } }),
}
const authModule = await import("../apps/web/src/lib/auth-controller.ts")
mock.module("../apps/web/src/lib/auth-controller.ts", () => ({
  ...authModule,
  getAuthController: () => auth,
}))
const { api } = await import("../apps/web/src/lib/api.ts")

afterEach(() => {
  globalThis.fetch = originalFetch
})

test("new chats never submit a legacy folder argument", async () => {
  let sent
  globalThis.fetch = async (url, init) => {
    sent = { url, init }
    return Response.json({ id: "new-chat" })
  }
  const session = await api.createSession("/private/old-folder")
  expect(session.id).toBe("new-chat")
  expect(sent.url).toBe("/api/sessions")
  expect(sent.init.method).toBe("POST")
  expect(sent.init.body).toBeUndefined()
  expect(sent.init.headers.get("Authorization")).toBe("Bearer test-token")
})
