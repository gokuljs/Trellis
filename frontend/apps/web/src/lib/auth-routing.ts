import { parseAppRoute, type AppPath } from "./app-routes"
import type { AuthState } from "./auth-controller"
import {
  consumeAuthReturnPath,
  rememberAuthReturnPath,
  type ReturnPathStorage,
} from "./auth-return"

export function hasAuthCallback(href: string): boolean {
  const url = new URL(href)
  const fragment = new URLSearchParams(url.hash.slice(1))
  return ["code", "error", "error_code"].some(
    (key) => url.searchParams.has(key) || fragment.has(key)
  )
}

export function authRedirect(
  pathname: string,
  auth: Pick<AuthState, "status" | "user" | "error"> & {
    callbackPending?: boolean
  },
  storage?: ReturnPathStorage
): AppPath | null {
  // Session restoration must consume OAuth parameters before changing the URL.
  if (auth.status === "loading") return null
  const route = parseAppRoute(pathname)
  if (route.kind === "unknown") return "/"
  if (route.path !== pathname) return route.path

  const authPage = route.kind === "login" || route.kind === "signup"
  if (auth.status === "signed-in" && auth.user) {
    if (authPage || route.kind === "home") {
      return consumeAuthReturnPath(storage) ?? (authPage ? "/app" : null)
    }
    return null
  }
  if (route.kind === "workspace" || route.kind === "chat") {
    rememberAuthReturnPath(route.path, storage)
    return "/login"
  }
  if (
    route.kind === "home" &&
    auth.callbackPending &&
    auth.error &&
    auth.status !== "configuration-error"
  ) {
    return "/login"
  }
  return null
}
