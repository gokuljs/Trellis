import { parseAppRoute, type AppPath } from "./app-routes"

export type ReturnPathStorage = Pick<
  Storage,
  "getItem" | "setItem" | "removeItem"
>
const returnPathKey = "trellis.auth-return-path"

function validatedReturnPath(value: string | null): AppPath | null {
  if (value === null) return null
  if (value === "/") return "/app"
  const route = parseAppRoute(value)
  return route.kind === "chat" || route.kind === "workspace" ? route.path : null
}

export function ensureAuthReturnPath(storage?: ReturnPathStorage): void {
  try {
    const target = storage ?? window.sessionStorage
    if (!validatedReturnPath(target.getItem(returnPathKey))) {
      target.setItem(returnPathKey, "/app")
    }
  } catch {
    // Browsers may block session storage; sign-in can still continue.
  }
}

export function rememberAuthReturnPath(
  pathname: string,
  storage?: ReturnPathStorage
): void {
  const path = validatedReturnPath(pathname)
  if (!path) return
  try {
    ;(storage ?? window.sessionStorage).setItem(returnPathKey, path)
  } catch {
    // Browsers may block session storage; sign-in can still continue.
  }
}

export function consumeAuthReturnPath(
  storage?: ReturnPathStorage
): AppPath | null {
  try {
    const target = storage ?? window.sessionStorage
    const saved = target.getItem(returnPathKey)
    target.removeItem(returnPathKey)
    return validatedReturnPath(saved)
  } catch {
    return null
  }
}
