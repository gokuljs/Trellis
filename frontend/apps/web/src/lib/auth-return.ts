import { parseAppRoute, type AppPath } from "./app-routes"

type ReturnPathStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">
const returnPathKey = "trellis.auth-return-path"

function validatedReturnPath(value: string | null): AppPath | null {
  if (value === null) return null
  const route = parseAppRoute(value)
  return route.kind === "chat" || route.kind === "home" ? route.path : null
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
