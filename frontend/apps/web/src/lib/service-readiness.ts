import {
  configuredApiUrl,
  configuredDatabaseReadiness,
} from "@/lib/service-targets"

export class ServiceReadinessError extends Error {
  readonly code: "service_unavailable" | "database_not_ready"

  constructor(
    code: "service_unavailable" | "database_not_ready" = "service_unavailable"
  ) {
    super(
      code === "database_not_ready"
        ? "The Trellis API is running, but its database is not ready. Check the backend logs and apply any pending Supabase migrations, then try again."
        : "Trellis is still starting or cannot be reached. Please try again shortly."
    )
    this.name = "ServiceReadinessError"
    this.code = code
  }
}

type ReadinessDependencies = {
  hosted?: boolean
  fetch?: typeof globalThis.fetch
  url?: () => string
  timeoutMs?: number
  probeTimeoutMs?: number
  retryDelaysMs?: readonly number[]
  readyTtlMs?: number
}

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort)
      resolve()
    }, milliseconds)
    signal.addEventListener("abort", abort, { once: true })
  })
}

export function createServiceReadiness({
  hosted,
  fetch: fetchRequest = (input, init) => globalThis.fetch(input, init),
  url,
  timeoutMs,
  probeTimeoutMs = 5_000,
  retryDelaysMs = [1_000, 2_000, 4_000, 5_000],
  readyTtlMs = 30_000,
}: ReadinessDependencies = {}) {
  let readyUntil = 0

  return async function waitUntilReady(signal: AbortSignal): Promise<void> {
    signal.throwIfAborted()
    if (Date.now() < readyUntil) return
    const databaseReadiness = hosted ?? configuredDatabaseReadiness()
    const probeUrl =
      url ?? (() => configuredApiUrl(databaseReadiness ? "/ready" : "/health"))
    const controller = new AbortController()
    const abort = () => controller.abort(signal.reason)
    signal.addEventListener("abort", abort, { once: true })
    const timer = setTimeout(
      () => controller.abort(new ServiceReadinessError()),
      timeoutMs ?? (databaseReadiness ? 90_000 : 10_000)
    )
    let attempt = 0
    try {
      while (true) {
        controller.signal.throwIfAborted()
        const probe = new AbortController()
        const stopProbe = () => probe.abort(controller.signal.reason)
        controller.signal.addEventListener("abort", stopProbe, { once: true })
        const probeTimer = setTimeout(() => probe.abort(), probeTimeoutMs)
        let response: Response | undefined
        let ready = false
        try {
          response = await Promise.race([
            (async () => {
              const result = await fetchRequest(probeUrl(), {
                signal: probe.signal,
                cache: "no-store",
                credentials: "omit",
                headers: { Accept: "application/json" },
              })
              if (
                result.status === 200 ||
                (databaseReadiness && result.status === 503)
              ) {
                let body: unknown
                try {
                  body = await result.json()
                } catch {
                  if (result.status === 200) throw new ServiceReadinessError()
                }
                if (
                  result.status === 503 &&
                  typeof body === "object" &&
                  body !== null &&
                  "status" in body &&
                  body.status === "unavailable"
                )
                  throw new ServiceReadinessError("database_not_ready")
                ready =
                  typeof body === "object" &&
                  body !== null &&
                  "status" in body &&
                  body.status === (databaseReadiness ? "ready" : "ok")
              }
              return result
            })(),
            new Promise<never>((_, reject) => {
              if (probe.signal.aborted) reject(probe.signal.reason)
              else
                probe.signal.addEventListener(
                  "abort",
                  () => reject(probe.signal.reason),
                  { once: true }
                )
            }),
          ])
        } catch (error) {
          controller.signal.throwIfAborted()
          if (error instanceof ServiceReadinessError) throw error
        } finally {
          clearTimeout(probeTimer)
          controller.signal.removeEventListener("abort", stopProbe)
        }
        controller.signal.throwIfAborted()
        if (response?.status === 200) {
          if (!ready) throw new ServiceReadinessError()
          readyUntil = Date.now() + readyTtlMs
          return
        }
        if (response && ![408, 429, 502, 503, 504].includes(response.status))
          throw new ServiceReadinessError()
        await pause(
          retryDelaysMs[Math.min(attempt++, retryDelaysMs.length - 1)] ?? 5_000,
          controller.signal
        )
      }
    } finally {
      clearTimeout(timer)
      signal.removeEventListener("abort", abort)
    }
  }
}

export const waitUntilServiceReady = createServiceReadiness()
