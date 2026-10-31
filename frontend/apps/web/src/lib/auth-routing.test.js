import assert from "node:assert/strict"
import { test } from "bun:test"

import { chatPath, parseAppRoute } from "./app-routes"
import { authRedirect, hasAuthCallback } from "./auth-routing"
import {
  consumeAuthReturnPath,
  ensureAuthReturnPath,
  rememberAuthReturnPath,
} from "./auth-return"

const chat = "/chats/29b96351-a37d-44b2-ae6e-1ccdbd210b4c"
const signedOut = { status: "signed-out", user: null, error: null }
const signedIn = { status: "signed-in", user: { id: "user" }, error: null }

function storage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  }
}

test("the public homepage and authenticated workspace have distinct routes", () => {
  assert.equal(parseAppRoute("/").kind, "home")
  assert.equal(parseAppRoute("/app").kind, "workspace")
  assert.equal(parseAppRoute("/app/other").kind, "unknown")
  assert.equal(chatPath(chat.slice(7).toUpperCase()), chat)
})

test("the homepage stays public regardless of session or missing configuration", () => {
  for (const auth of [
    signedOut,
    signedIn,
    {
      status: "configuration-error",
      user: null,
      error: "Missing configuration",
    },
  ]) {
    assert.equal(authRedirect("/", auth, storage()), null)
  }
})

test("workspace sign-in returns to the workspace after the root callback", () => {
  const session = storage()
  assert.equal(authRedirect("/app", signedOut, session), "/login")
  assert.equal(
    authRedirect("/", { ...signedOut, status: "loading" }, session),
    null
  )
  assert.equal(authRedirect("/", signedIn, session), "/app")
  assert.equal(authRedirect("/", signedIn, session), null)
})

test("starting OAuth preserves a deep chat destination", () => {
  const session = storage()
  assert.equal(authRedirect(chat, signedOut, session), "/login")
  ensureAuthReturnPath(session)
  assert.equal(authRedirect("/", signedIn, session), chat)
})

test("direct sign-in and sign-up open the workspace", () => {
  for (const route of ["/login", "/signup"]) {
    const session = storage()
    assert.equal(authRedirect(route, signedIn, session), "/app")
    ensureAuthReturnPath(session)
    assert.equal(authRedirect("/", signedIn, session), "/app")
  }
})

test("OAuth failure at the root opens the visible retry screen", () => {
  assert.equal(
    authRedirect(
      "/",
      { ...signedOut, error: "Sign-in was denied", callbackPending: true },
      storage()
    ),
    "/login"
  )
  assert.equal(
    authRedirect(
      "/",
      {
        ...signedOut,
        status: "error",
        error: "Timed out",
        callbackPending: true,
      },
      storage()
    ),
    "/login"
  )
})

test("callback intent comes from OAuth query or fragment parameters", () => {
  for (const suffix of [
    "?code=auth-code",
    "?error=access_denied",
    "#error_code=expired",
  ]) {
    assert.equal(hasAuthCallback(`https://trellis.example/${suffix}`), true)
  }
  assert.equal(hasAuthCallback("https://trellis.example/"), false)
  assert.equal(
    hasAuthCallback("https://trellis.example/?ref=source#about"),
    false
  )
})

test("an expired or failed sign-in does not make the normal homepage private", () => {
  for (const error of ["Your session expired", "Sign-in was denied"]) {
    assert.equal(authRedirect("/", { ...signedOut, error }, storage()), null)
  }
})

test("return paths reject external and authentication routes and migrate the old root", () => {
  for (const path of [
    "https://example.com",
    "//example.com",
    "/login",
    "/signup",
    "/chats/invalid",
  ]) {
    const session = storage()
    rememberAuthReturnPath(path, session)
    assert.equal(consumeAuthReturnPath(session), null)
  }
  const session = storage()
  session.setItem("trellis.auth-return-path", "/")
  assert.equal(consumeAuthReturnPath(session), "/app")
  assert.equal(consumeAuthReturnPath(session), null)
})

test("blocked session storage does not prevent navigation", () => {
  const session = {
    getItem() {
      throw new Error("Blocked")
    },
    setItem() {
      throw new Error("Blocked")
    },
    removeItem() {
      throw new Error("Blocked")
    },
  }
  assert.doesNotThrow(() => ensureAuthReturnPath(session))
  assert.equal(authRedirect("/app", signedOut, session), "/login")
  assert.equal(authRedirect("/login", signedIn, session), "/app")
})
