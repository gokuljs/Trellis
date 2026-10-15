import { queryOptions, type QueryClient } from "@tanstack/react-query"

import { api } from "@/lib/api"
import type { Session, SessionDetail } from "@/lib/app-types"

export const serverKeys = {
  settings: ["settings"] as const,
  sessions: ["sessions"] as const,
  session: (sessionId: string) => ["session", sessionId] as const,
}

export function settingsQueryOptions(
  service: Pick<typeof api, "getSettings"> = api
) {
  return queryOptions({
    queryKey: serverKeys.settings,
    refetchInterval: 10 * 60_000,
    queryFn: ({ signal }) => service.getSettings({ signal }),
  })
}

export function sessionModelMutationOptions(
  client: QueryClient,
  service: Pick<typeof api, "selectSessionModel"> = api
) {
  return {
    mutationKey: ["session-model"],
    mutationFn: ({
      sessionId,
      modelId,
    }: {
      sessionId: string
      modelId: string
    }) => service.selectSessionModel(sessionId, modelId),
    onMutate: ({ sessionId }: { sessionId: string }) =>
      client.cancelQueries({ queryKey: serverKeys.session(sessionId) }),
    onSuccess: (session: Session) => {
      client.setQueryData<SessionDetail>(
        serverKeys.session(session.id),
        (detail) => (detail ? { ...detail, session } : undefined)
      )
      client.setQueryData<Session[]>(serverKeys.sessions, (sessions) =>
        sessions?.map((current) =>
          current.id === session.id ? session : current
        )
      )
      void client.invalidateQueries({
        queryKey: ["context-preview", session.id],
      })
    },
  }
}
