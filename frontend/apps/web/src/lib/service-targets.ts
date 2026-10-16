import { apiUrl, parseBackendTargets } from "@/lib/backend-targets"

function getBackendTargets() {
  return parseBackendTargets(
    import.meta.env ?? {},
    import.meta.env?.PROD === true
  )
}

export function configuredApiUrl(path: string): string {
  return apiUrl(path, getBackendTargets())
}

export function runtimeUrl(): string {
  const configured = getBackendTargets().runtimeWebSocketUrl
  if (configured) return configured
  const url = new URL("/api/runtime", window.location.href)
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  return url.href
}
