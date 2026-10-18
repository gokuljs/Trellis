import {
  infiniteQueryOptions,
  queryOptions,
  type QueryClient,
  type InfiniteData,
  type MutationFilters,
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
    meta: { telemetryOperation: "settings.load" },
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
    meta: { telemetryOperation: "context.preview" },
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
    meta: {
      telemetryOperation: "settings.save" as const,
      telemetryOwner: "caller" as const,
    },
    mutationKey: ["session-model"],
    scope: { id: "session-model-preference" },
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
    onSuccess: async (session: Session) => {
      // Navigation can start another read after onMutate cancelled the old ones.
      // Let those snapshots settle before publishing the saved preference.
      await Promise.allSettled(
        [
          serverKeys.history(session.id),
          serverKeys.session(session.id),
          serverKeys.sessionPages,
          serverKeys.sessions,
        ].map(
          (queryKey) =>
            client.getQueryCache().find({ queryKey, exact: true })?.promise
        )
      )
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

export function sessionModelMutationFilters(
  sessionId: string | null
): MutationFilters {
  return {
    mutationKey: ["session-model"],
    predicate: (mutation) =>
      sessionId !== null &&
      (mutation.state.variables as { sessionId: string } | undefined)
        ?.sessionId === sessionId,
  }
}

export function sessionHistoryQueryOptions(
  sessionId: string,
  service: Pick<typeof api, "getSessionHistory"> = api
) {
  return infiniteQueryOptions({
    meta: { telemetryOperation: "history.load", telemetryOwner: "caller" },
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
    meta: { telemetryOperation: "sessions.list" },
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
  void client.cancelQueries({ queryKey: serverKeys.sessionPages, exact: true })
  let overflow = false
  client.setQueryData<InfiniteData<SessionPage>>(
    serverKeys.sessionPages,
    (current) => {
      if (!current) return undefined
      overflow = current.pages.some((page) => page.items.length > 40)
      const exists = current.pages.some((page) =>
        page.items.some((item) => item.id === session.id)
      )
      if (exists)
        return {
          ...current,
          pages: current.pages.map((page) => ({
            ...page,
            items: page.items.map((item) =>
              item.id === session.id ? session : item
            ),
          })),
        }

      const timestampMicros = (value: string) => {
        const fraction = value.match(/\.(\d+)/)?.[1] ?? ""
        return (
          Date.parse(value) * 1000 + Number(fraction.padEnd(6, "0").slice(3, 6))
        )
      }
      const updatedAt = timestampMicros(session.updated_at)
      const comesAfter = (item: Session) => {
        const itemUpdatedAt = timestampMicros(item.updated_at)
        return (
          updatedAt > itemUpdatedAt ||
          (updatedAt === itemUpdatedAt && session.id > item.id)
        )
      }
      const foundPage = current.pages.findIndex((page) =>
        page.items.some(comesAfter)
      )
      const pageIndex = foundPage < 0 ? current.pages.length - 1 : foundPage
      const page = current.pages[pageIndex]
      const foundItem = page.items.findIndex(comesAfter)
      const itemIndex = foundItem < 0 ? page.items.length : foundItem
      const items = page.items.toSpliced(itemIndex, 0, session)
      overflow ||= items.length > 40
      return {
        ...current,
        pages: current.pages.map((page, index) =>
          index === pageIndex ? { ...page, items } : page
        ),
        pageParams:
          pageIndex === 0 && itemIndex === 0
            ? [null, ...current.pageParams.slice(1)]
            : current.pageParams,
      }
    }
  )
  if (overflow)
    void client.invalidateQueries({
      queryKey: serverKeys.sessionPages,
      exact: true,
    })
}

export function runSummariesQueryOptions(
  sessionId: string,
  service: Pick<typeof api, "listRunSummaries"> = api,
  turnId?: string
) {
  return infiniteQueryOptions({
    meta: {
      telemetryOperation: "activity.summaries",
      telemetryOwner: turnId === undefined ? "caller" : "cache",
    },
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
    meta: { telemetryOperation: "activity.events" },
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
