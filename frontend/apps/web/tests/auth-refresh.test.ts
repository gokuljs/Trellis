import assert from "node:assert/strict"
import type { Session, SupabaseClient } from "@supabase/supabase-js"
import { afterEach, test } from "bun:test"

import {
  AuthAccessError,
  createAuthController,
} from "../src/lib/auth-controller"

const originalWindow = globalThis.window
const user = { id: "alice" }
const session = { user, access_token: "original" } as Session

afterEach(() => {
  globalThis.window = originalWindow
})

async function setup(refresh: () => Promise<unknown>) {
  globalThis.window = {
    location: { href: "https://trellis.test/" },
    addEventListener() {},
    removeEventListener() {},
  } as unknown as Window & typeof globalThis
  let event: (name: string, value: Session | null) => void = () => {}
  const controller = createAuthController({
    auth: {
      getSession: async () => ({ data: { session }, error: null }),
      getUser: async () => ({ data: { user }, error: null }),
      refreshSession: refresh,
      signOut: async () => ({ error: null }),
      onAuthStateChange: (listener: typeof event) => {
        event = listener
        return { data: { subscription: { unsubscribe() {} } } }
      },
    },
  } as unknown as SupabaseClient)
  const unsubscribe = controller.subscribe(() => {})
  for (
    let attempt = 0;
    attempt < 20 && controller.getSnapshot().status !== "signed-in";
    attempt++
  )
    await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(controller.getSnapshot().status, "signed-in")
  return {
    controller,
    unsubscribe,
    event: (name: string, value: Session | null) => event(name, value),
  }
}

test("forced refresh returns a new credential without changing the signed-in account epoch", async () => {
  const { controller, unsubscribe, event } = await setup(async () => {
    const refreshed = { ...session, access_token: "refreshed" }
    event("TOKEN_REFRESHED", refreshed)
    return { data: { session: refreshed }, error: null }
  })
  try {
    const epoch = controller.getSessionEpoch()
    assert.deepEqual(await controller.refreshAccessToken(), {
      token: "refreshed",
      userId: "alice",
      sessionEpoch: epoch,
    })
    assert.equal(controller.getSnapshot().status, "signed-in")
    assert.equal(controller.getSessionEpoch(), epoch)
  } finally {
    unsubscribe()
  }
})

test("temporary refresh errors leave the account signed in", async () => {
  const { controller, unsubscribe } = await setup(async () => ({
    data: { session: null },
    error: new Error("Network offline"),
  }))
  try {
    await assert.rejects(
      controller.refreshAccessToken(),
      (error: unknown) =>
        error instanceof AuthAccessError && error.code === "auth_unavailable"
    )
    assert.equal(controller.getSnapshot().status, "signed-in")
    assert.equal(controller.getSnapshot().user?.id, "alice")
  } finally {
    unsubscribe()
  }
})

test("a refresh that finishes after sign-out cannot restore the old account", async () => {
  let complete!: (value: unknown) => void
  const { controller, unsubscribe, event } = await setup(
    () =>
      new Promise((resolve) => {
        complete = resolve
      })
  )
  try {
    const refreshing = controller.refreshAccessToken()
    const rejected = assert.rejects(
      refreshing,
      (error: unknown) =>
        error instanceof AuthAccessError && error.code === "reauth_required"
    )
    await new Promise((resolve) => setTimeout(resolve, 0))
    await controller.signOut()
    event("TOKEN_REFRESHED", { ...session, access_token: "late" })
    complete({
      data: { session: { ...session, access_token: "late" } },
      error: null,
    })
    await rejected
    assert.equal(controller.getSnapshot().status, "signed-out")
  } finally {
    unsubscribe()
  }
})
