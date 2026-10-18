import { useSyncExternalStore } from "react"

export type AppPath = "/" | "/login" | "/signup" | `/chats/${string}`
export type AppRoute =
  | { kind: "home"; path: "/" }
  | { kind: "login"; path: "/login" }
  | { kind: "signup"; path: "/signup" }
  | { kind: "chat"; chatId: string; path: `/chats/${string}` }
  | { kind: "unknown" }

const uuid = /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i

export function chatPath(chatId: string): `/chats/${string}` {
  if (!uuid.test(chatId)) throw new Error("Invalid chat ID")
  return `/chats/${chatId.toLowerCase()}`
}

export function parseAppRoute(pathname: string): AppRoute {
  if (pathname === "/") return { kind: "home", path: "/" }
  if (pathname === "/login") return { kind: "login", path: "/login" }
  if (pathname === "/signup") return { kind: "signup", path: "/signup" }
  const match = /^\/chats\/([^/]+)$/.exec(pathname)
  if (match && uuid.test(match[1]!)) {
    const chatId = match[1]!.toLowerCase()
    return { kind: "chat", chatId, path: chatPath(chatId) }
  }
  return { kind: "unknown" }
}

export function subscribeToPath(listener: () => void) {
  window.addEventListener("popstate", listener)
  return () => window.removeEventListener("popstate", listener)
}

export function currentPath() {
  return window.location.pathname
}

export function useAppPath() {
  return useSyncExternalStore(subscribeToPath, currentPath, currentPath)
}

export function navigate(path: AppPath, replace = false): void {
  const route = parseAppRoute(path)
  if (route.kind === "unknown") throw new Error("Invalid application route")
  const samePath = currentPath() === route.path
  if (samePath && !window.location.search && !window.location.hash) return
  if (replace || samePath) window.history.replaceState(null, "", route.path)
  else window.history.pushState(null, "", route.path)
  window.dispatchEvent(new Event("popstate"))
}
