import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query"

import { ApiError } from "@/lib/api"
import { reportFailure } from "@/lib/telemetry"

const disposedClients = new WeakSet<QueryClient>()

function retryRead(failureCount: number, error: Error): boolean {
  if (failureCount >= 1 || error.name === "AbortError") return false
  if (!(error instanceof ApiError)) return false
  if (
    /auth|reauth|cloud_access_denied|account_in_use/.test(error.code) ||
    (error.status >= 400 && error.status < 500)
  )
    return false
  return error.status === 0 || error.status >= 500
}

export function createSessionQueryClient(): QueryClient {
  const client = new QueryClient({
    queryCache: new QueryCache({
      onError(error, query) {
        const operation = query.meta?.telemetryOperation
        if (
          operation &&
          query.meta?.telemetryOwner !== "caller" &&
          !disposedClients.has(client)
        )
          reportFailure(error, operation)
      },
    }),
    mutationCache: new MutationCache({
      onError(error, _variables, _context, mutation) {
        const operation = mutation.meta?.telemetryOperation
        if (
          operation &&
          mutation.meta?.telemetryOwner !== "caller" &&
          !disposedClients.has(client)
        )
          reportFailure(error, operation)
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        retry: retryRead,
        retryDelay: 750,
        refetchOnWindowFocus: false,
        networkMode: "always",
      },
      mutations: {
        retry: false,
        gcTime: 5 * 60_000,
        networkMode: "always",
      },
    },
  })
  return client
}

export function disposeSessionQueryClient(client: QueryClient): void {
  disposedClients.add(client)
  void client.cancelQueries({}, { silent: true })
  client.clear()
}
