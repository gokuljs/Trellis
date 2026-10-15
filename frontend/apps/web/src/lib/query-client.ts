import { QueryClient } from "@tanstack/react-query"

import { ApiError } from "@/lib/api"

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
  return new QueryClient({
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
}

export function disposeSessionQueryClient(client: QueryClient): void {
  void client.cancelQueries({}, { silent: true })
  client.clear()
}
