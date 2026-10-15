import {
  infiniteQueryOptions,
  queryOptions,
  type QueryClient,
  type InfiniteData,
} from "@tanstack/react-query"

import { ApiError, api } from "@/lib/api"
import type {
  ContextPreviewRequest,
  Session,
  SessionDetail,
  SessionHistoryPage,
  SessionPage,
} from "@/lib/app-types"
import { parseContextPreview, previewDraftTooLong } from "@/lib/context-gauge"

export const serverKeys = {
  settings: ["settings"] as const,
  sessions: ["sessions"] as const,
  history: (sessionId: string) => ["session-history", sessionId] as const,
  sessionPages: ["session-pages"] as const,
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
      Promise.all([
        client.cancelQueries({ queryKey: serverKeys.session(sessionId) }),
        client.cancelQueries({ queryKey: serverKeys.history(sessionId) }),
        client.cancelQueries({ queryKey: serverKeys.sessionPages }),
      ]),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: serverKeys.sessionPages })
    },
    onSuccess: (session: Session) => {
      client.setQueryData<InfiniteData<SessionHistoryPage>>(
        serverKeys.history(session.id),
        (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({ ...page, session })),
              }
            : undefined
      )
      cacheSession(client, session)
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

export function sessionHistoryQueryOptions(
  sessionId: string,
  service: Pick<typeof api, "getSessionHistory"> = api
) {
  return infiniteQueryOptions({
    queryKey: serverKeys.history(sessionId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      service.getSessionHistory(sessionId, pageParam, { signal }),
    getNextPageParam: (lastPage, _pages, lastCursor) =>
      lastPage.next_before_cursor === lastCursor
        ? undefined
        : (lastPage.next_before_cursor ?? undefined),
    maxPages: 5,
    gcTime: 0,
  })
}

export async function resetHistoryToLatest(
  client: QueryClient,
  sessionId: string,
  service: Pick<typeof api, "getSessionHistory"> = api
) {
  const options = sessionHistoryQueryOptions(sessionId, service)
  await client.cancelQueries({ queryKey: options.queryKey, exact: true })
  await client.resetQueries(
    { queryKey: options.queryKey, exact: true },
    { cancelRefetch: true }
  )
  return client.fetchInfiniteQuery({ ...options, staleTime: 0 })
}

export function sessionPagesQueryOptions(
  service: Pick<typeof api, "listSessionPages"> = api
) {
  return infiniteQueryOptions({
    queryKey: serverKeys.sessionPages,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      service.listSessionPages(pageParam, { signal }),
    getNextPageParam: (lastPage, _pages, lastCursor) =>
      lastPage.next_before_cursor === lastCursor
        ? undefined
        : (lastPage.next_before_cursor ?? undefined),
    maxPages: 5,
  })
}

export function cacheSession(client: QueryClient, session: Session) {
  const data = client.getQueryData<InfiniteData<SessionPage>>(
    serverKeys.sessionPages
  )
  if (!data) {
    void client.invalidateQueries({ queryKey: serverKeys.sessionPages })
    return
  }
  const exists = data.pages.some((page) =>
    page.items.some((item) => item.id === session.id)
  )
  if (!exists) {
    void client.resetQueries({ queryKey: serverKeys.sessionPages, exact: true })
    return
  }
  client.setQueryData<InfiniteData<SessionPage>>(
    serverKeys.sessionPages,
    (current) =>
      current
        ? {
            ...current,
            pages: current.pages.map((page) => ({
              ...page,
              items: page.items.map((item) =>
                item.id === session.id ? session : item
              ),
            })),
          }
        : undefined
  )
}

export function runSummariesQueryOptions(
  sessionId: string,
  service: Pick<typeof api, "listRunSummaries"> = api,
  turnId?: string
) {
  return infiniteQueryOptions({
    queryKey: ["run-summaries", sessionId, turnId ?? null, "newest-first"],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      service.listRunSummaries(
        sessionId,
        pageParam,
        { signal },
        { turnId, newestFirst: true }
      ),
    getNextPageParam: (lastPage, _pages, lastOffset) =>
      lastPage.next_offset !== null && lastPage.next_offset > lastOffset
        ? lastPage.next_offset
        : undefined,
    maxPages: 5,
    gcTime: 0,
  })
}

export function runEventsQueryOptions(
  sessionId: string,
  runId: string,
  service: Pick<typeof api, "listRunEvents"> = api
) {
  return infiniteQueryOptions({
    queryKey: ["run-events", sessionId, runId],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      service.listRunEvents(sessionId, runId, pageParam, { signal }),
    getNextPageParam: (lastPage, _pages, lastSequence) =>
      lastPage.next_after_sequence !== null &&
      lastPage.next_after_sequence > lastSequence
        ? lastPage.next_after_sequence
        : undefined,
    maxPages: 5,
    gcTime: 0,
  })
}
