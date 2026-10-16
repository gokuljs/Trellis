export type BackendEnvironment = {
  VITE_API_BASE_URL?: string
  VITE_RUNTIME_WS_URL?: string
}

export type BackendTargets = {
  apiOrigin: string | null
  runtimeWebSocketUrl: string | null
}

function isLocalTarget(url: URL): boolean {
  return ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
}

export function requiresDatabaseReadiness(
  targets: BackendTargets,
  production: boolean
): boolean {
  return (
    production ||
    (targets.apiOrigin !== null && !isLocalTarget(new URL(targets.apiOrigin)))
  )
}

export function parseBackendTargets(
  environment: BackendEnvironment,
  requireExplicit: boolean
): BackendTargets {
  const apiOrigin = environment.VITE_API_BASE_URL?.trim()
  const runtimeWebSocketUrl = environment.VITE_RUNTIME_WS_URL?.trim()
  if (!apiOrigin && !runtimeWebSocketUrl && !requireExplicit)
    return { apiOrigin: null, runtimeWebSocketUrl: null }
  if (!apiOrigin || !runtimeWebSocketUrl)
    throw new Error(
      "Set VITE_API_BASE_URL and VITE_RUNTIME_WS_URL for this deployment environment."
    )

  let api: URL
  let runtime: URL
  try {
    api = new URL(apiOrigin)
    runtime = new URL(runtimeWebSocketUrl)
  } catch {
    throw new Error(
      "The Trellis API and WebSocket addresses must be valid URLs."
    )
  }
  if (
    api.origin !== apiOrigin ||
    api.username ||
    api.password ||
    api.pathname !== "/" ||
    api.search ||
    api.hash ||
    (api.protocol !== "https:" &&
      (requireExplicit || api.protocol !== "http:" || !isLocalTarget(api))) ||
    (requireExplicit && isLocalTarget(api))
  )
    throw new Error(
      "VITE_API_BASE_URL must be a canonical HTTPS origin without a path, trailing slash, or credentials."
    )
  const expectedRuntime = new URL("/api/runtime", api)
  expectedRuntime.protocol = api.protocol === "https:" ? "wss:" : "ws:"
  if (
    runtime.href !== expectedRuntime.href ||
    runtimeWebSocketUrl !== runtime.href
  )
    throw new Error(
      "VITE_RUNTIME_WS_URL must point directly to /api/runtime on the configured API origin using WSS."
    )
  return { apiOrigin, runtimeWebSocketUrl }
}

export function apiUrl(path: string, targets: BackendTargets): string {
  return targets.apiOrigin ? new URL(path, targets.apiOrigin).href : path
}
