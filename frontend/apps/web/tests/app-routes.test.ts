import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"

import {
  chatPath,
  currentPath,
  navigate,
  parseAppRoute,
  subscribeToPath,
} from "../src/lib/app-routes"
import {
  consumeAuthReturnPath,
  rememberAuthReturnPath,
} from "../src/lib/auth-return"

const chatId = "11111111-2222-4333-a444-555555555555"
const originalWindow = globalThis.window

afterEach(() => {
  globalThis.window = originalWindow
})

function browser(path = "/") {
  const events = new EventTarget()
  const entries = [path]
  let index = 0
  const location = { pathname: path, search: "", hash: "" }
  const visit = (value: string | URL | null | undefined) => {
    const url = new URL(String(value), "https://trellis.test")
    location.pathname = url.pathname
    location.search = url.search
    location.hash = url.hash
  }
  globalThis.window = {
    location,
    history: {
      pushState(_state: unknown, _title: string, value: string) {
        entries.splice(++index, entries.length, value)
        visit(value)
      },
      replaceState(_state: unknown, _title: string, value: string) {
        entries[index] = value
        visit(value)
      },
    },
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
  } as unknown as Window & typeof globalThis
  return {
    entries,
    back() {
      if (index > 0) visit(entries[--index])
      events.dispatchEvent(new Event("popstate"))
    },
    forward() {
      if (index + 1 < entries.length) visit(entries[++index])
      events.dispatchEvent(new Event("popstate"))
    },
  }
}

function storage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  }
}

test("routes parse exact supported paths and canonicalize UUIDs", () => {
  assert.deepEqual(parseAppRoute("/"), { kind: "home", path: "/" })
  assert.deepEqual(parseAppRoute("/login"), { kind: "login", path: "/login" })
  assert.deepEqual(parseAppRoute("/signup"), {
    kind: "signup",
    path: "/signup",
  })
  assert.deepEqual(parseAppRoute(`/chats/${chatId.toUpperCase()}`), {
    kind: "chat",
    chatId,
    path: `/chats/${chatId}`,
  })
  assert.equal(chatPath(chatId.toUpperCase()), `/chats/${chatId}`)
  for (const path of [
    "/chats/not-a-uuid",
    `/chats/${chatId}/`,
    `/chats/${chatId}/extra`,
    `/chats/${chatId}?token=secret`,
    "/settings",
    "//evil.test/",
  ]) {
    assert.deepEqual(parseAppRoute(path), { kind: "unknown" })
  }
})

test("history pushes chats once, replaces a new draft, and restores Back/Forward", () => {
  const history = browser()
  const paths: string[] = []
  const unsubscribe = subscribeToPath(() => paths.push(currentPath()))
  navigate(chatPath(chatId))
  navigate(chatPath(chatId))
  navigate("/")
  const created = "aaaaaaaa-2222-4333-a444-555555555555"
  navigate(chatPath(created), true)
  assert.deepEqual(history.entries, [
    "/",
    `/chats/${chatId}`,
    `/chats/${created}`,
  ])
  history.back()
  assert.equal(currentPath(), `/chats/${chatId}`)
  history.forward()
  assert.equal(currentPath(), `/chats/${created}`)
  assert.equal(paths.length, 5)
  unsubscribe()
  history.back()
  assert.equal(paths.length, 5)
})

test("navigation clears query strings and fragments without a new history entry", () => {
  const history = browser(`/chats/${chatId}`)
  window.location.search = "?code=private"
  window.location.hash = "#private"
  navigate(chatPath(chatId))
  assert.deepEqual(history.entries, [`/chats/${chatId}`])
  assert.equal(window.location.search, "")
  assert.equal(window.location.hash, "")
})

test("auth return stores a canonical pathname and consumes it once", () => {
  const saved = storage()
  rememberAuthReturnPath(`/chats/${chatId.toUpperCase()}`, saved)
  assert.equal(consumeAuthReturnPath(saved), `/chats/${chatId}`)
  assert.equal(consumeAuthReturnPath(saved), null)
})

test("auth return ignores auth pages and rejects external or decorated paths", () => {
  const saved = storage()
  rememberAuthReturnPath(chatPath(chatId), saved)
  rememberAuthReturnPath("/login", saved)
  rememberAuthReturnPath("/signup", saved)
  assert.equal(consumeAuthReturnPath(saved), chatPath(chatId))
  for (const path of [
    "https://evil.test/",
    "//evil.test/",
    `/chats/${chatId}?secret=yes`,
    `/chats/${chatId}#secret`,
    "/unknown",
  ]) {
    rememberAuthReturnPath(path, saved)
    assert.equal(consumeAuthReturnPath(saved), null)
  }
})

test("auth return degrades gracefully when storage is blocked", () => {
  const blocked = {
    getItem() {
      throw new Error("blocked")
    },
    setItem() {
      throw new Error("blocked")
    },
    removeItem() {
      throw new Error("blocked")
    },
  }
  assert.doesNotThrow(() => rememberAuthReturnPath(chatPath(chatId), blocked))
  assert.equal(consumeAuthReturnPath(blocked), null)
})
