import { queryOptions, type QueryClient } from "@tanstack/react-query"

import { ApiError, api } from "@/lib/api"
import type {
  ContextPreviewRequest,
  Session,
  SessionDetail,
} from "@/lib/app-types"
import { parseContextPreview, previewDraftTooLong } from "@/lib/context-gauge"

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
    queryFn: ({ signal }) => service.getSettings({ signal }),
    refetchInterval: 10 * 60_000,
  })
}

export function contextPreviewQueryOptions(
  input: ContextPreviewRequest,
  generation: number,
  service: Pick<typeof api, "previewContext"> = api
) {
  const tooLong = previewDraftTooLong(input.draft)
  return queryOptions({
    queryKey: [
      "context-preview",
      input.session_id ?? null,
      input.model_id,
      tooLong ? "draft-too-long" : input.draft,
      input.budget_preset,
      generation,
    ],
    gcTime: 0,
    staleTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      if (tooLong)
        throw new ApiError(
          "context_preview_draft_too_long",
          "Context preview supports drafts up to 32,000 characters.",
          400
        )
      const preview = parseContextPreview(
        await service.previewContext(input, { signal })
      )
      if (!preview || preview.model_id !== input.model_id)
        throw new ApiError(
          "context_preview_unavailable",
          "Context usage is unavailable.",
          0
        )
      return preview
    },
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
