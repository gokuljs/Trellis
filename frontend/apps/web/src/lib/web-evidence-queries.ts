import {
  infiniteQueryOptions,
  queryOptions,
  type QueryClient,
} from "@tanstack/react-query"

import { api } from "@/lib/api"

export const webEvidenceKeys = {
  scope: (ownerId: string, sessionId: string) =>
    ["web-evidence", ownerId, sessionId] as const,
  sources: (ownerId: string, sessionId: string, revision = 0) =>
    [
      ...webEvidenceKeys.scope(ownerId, sessionId),
      revision,
      "sources",
    ] as const,
  lines: (
    ownerId: string,
    sessionId: string,
    sourceId: string,
    start: number,
    count: number,
    revision = 0
  ) =>
    [
      ...webEvidenceKeys.scope(ownerId, sessionId),
      revision,
      "lines",
      sourceId,
      start,
      count,
    ] as const,
  citations: (
    ownerId: string,
    sessionId: string,
    callId: string,
    revision = 0
  ) =>
    [
      ...webEvidenceKeys.scope(ownerId, sessionId),
      revision,
      "citations",
      callId,
    ] as const,
}

export function webSourcesQueryOptions(
  ownerId: string,
  sessionId: string,
  revision = 0,
  service: Pick<typeof api, "listWebSources"> = api
) {
  return infiniteQueryOptions({
    queryKey: webEvidenceKeys.sources(ownerId, sessionId, revision),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      service.listWebSources(sessionId, pageParam, { signal }),
    getNextPageParam: (lastPage) => lastPage.next_offset ?? undefined,
    gcTime: 0,
    retry: false,
  })
}

export function webSourceLinesQueryOptions(
  ownerId: string,
  sessionId: string,
  sourceId: string,
  start: number,
  count: number,
  revision = 0,
  service: Pick<typeof api, "readWebSource"> = api
) {
  return queryOptions({
    queryKey: webEvidenceKeys.lines(
      ownerId,
      sessionId,
      sourceId,
      start,
      count,
      revision
    ),
    queryFn: ({ signal }) =>
      service.readWebSource(sessionId, sourceId, start, count, { signal }),
    gcTime: 0,
    staleTime: 0,
    retry: false,
  })
}

export function webCitationsQueryOptions(
  ownerId: string,
  sessionId: string,
  callId: string,
  revision = 0,
  service: Pick<typeof api, "readWebCitations"> = api
) {
  return queryOptions({
    queryKey: webEvidenceKeys.citations(ownerId, sessionId, callId, revision),
    queryFn: ({ signal }) =>
      service.readWebCitations(sessionId, callId, { signal }),
    gcTime: 0,
    retry: false,
  })
}

export async function discardWebEvidence(
  client: QueryClient,
  ownerId: string,
  sessionId: string
) {
  const queryKey = webEvidenceKeys.scope(ownerId, sessionId)
  await client.cancelQueries({ queryKey })
  client.removeQueries({ queryKey })
}
