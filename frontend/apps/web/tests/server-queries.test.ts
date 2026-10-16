import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"
import {
  InfiniteQueryObserver,
  isCancelledError,
  QueryClient,
  type InfiniteData,
} from "@tanstack/react-query"

import type { Session, SessionPage } from "../src/lib/app-types"
import {
  cacheSession,
  serverKeys,
  sessionPagesQueryOptions,
} from "../src/lib/server-queries"

const cleanups: (() => void)[] = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
})

function queryClient() {
  const client = new QueryClient()
  cleanups.push(() => client.clear())
  return client
}

function session(id: string, updatedAt = "2026-10-15T12:00:00Z"): Session {
  return {
    id,
    title: id,
    created_at: updatedAt,
    updated_at: updatedAt,
    message_count: 1,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function readPages(client: QueryClient) {
  return client.getQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages)
}

function ids(data: InfiniteData<SessionPage> | undefined) {
  return data?.pages.flatMap((page) => page.items.map((item) => item.id))
}

test("a new chat appears first without clearing the observed sidebar", () => {
  const client = queryClient()
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [{ items: [session("old")], next_before_cursor: null }],
    pageParams: [null],
  })
  const observer = new InfiniteQueryObserver(client, {
    ...sessionPagesQueryOptions(),
    staleTime: Infinity,
    enabled: false,
  })
  const snapshots: (string[] | undefined)[] = []
  const unsubscribe = observer.subscribe((result) => {
    snapshots.push(ids(result.data))
  })
  cleanups.push(unsubscribe)

  cacheSession(client, session("new", "2026-10-16T12:00:00Z"))

  assert.deepEqual(ids(readPages(client)), ["new", "old"])
  assert.deepEqual(snapshots, [["new", "old"]])
})

test("saved titles and models update the existing row without duplication", () => {
  const client = queryClient()
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [
      { items: [session("newer"), session("old")], next_before_cursor: null },
    ],
    pageParams: [null],
  })

  cacheSession(client, {
    ...session("old"),
    title: "Renamed",
    model_id: "model-2",
  })

  assert.deepEqual(ids(readPages(client)), ["newer", "old"])
  assert.deepEqual(readPages(client)?.pages[0]?.items[1], {
    ...session("old"),
    title: "Renamed",
    model_id: "model-2",
  })
})

test("inserting a new chat preserves older pages and their opaque cursors", () => {
  const client = queryClient()
  for (const firstCursor of [null, "evicted-newest-window"]) {
    const olderPage: SessionPage = {
      items: [session("older", "2026-10-14T12:00:00Z")],
      next_before_cursor: "next-opaque-cursor",
    }
    client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
      pages: [
        { items: [session("old")], next_before_cursor: "older-opaque-cursor" },
        olderPage,
      ],
      pageParams: [firstCursor, "older-opaque-cursor"],
    })
    const cachedOlderPage = readPages(client)?.pages[1]

    cacheSession(client, session("new", "2026-10-16T12:00:00Z"))

    const data = readPages(client)
    assert.deepEqual(ids(data), ["new", "old", "older"])
    assert.deepEqual(data?.pageParams, [null, "older-opaque-cursor"])
    assert.equal(data?.pages[0]?.next_before_cursor, "older-opaque-cursor")
    assert.equal(data?.pages[1], cachedOlderPage)
  }
})

test("uncached older chats keep the server's timestamp and id ordering", () => {
  const client = queryClient()
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [
      {
        items: [session("newer", "2026-10-16T12:00:00Z"), session("b")],
        next_before_cursor: "opaque-cursor",
      },
      {
        items: [session("oldest", "2026-10-14T12:00:00Z")],
        next_before_cursor: null,
      },
    ],
    pageParams: ["evicted-newest-window", "opaque-cursor"],
  })

  cacheSession(client, session("c"))
  cacheSession(client, session("a"))

  assert.deepEqual(ids(readPages(client)), ["newer", "c", "b", "a", "oldest"])
  assert.deepEqual(readPages(client)?.pageParams, [
    "evicted-newest-window",
    "opaque-cursor",
  ])
})

test("session ordering preserves microseconds and equivalent UTC offsets", () => {
  const client = queryClient()
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [
      {
        items: [session("older", "2026-10-15T12:00:00.000100Z")],
        next_before_cursor: null,
      },
    ],
    pageParams: [null],
  })

  cacheSession(client, session("newer", "2026-10-15T12:00:00.000900Z"))
  cacheSession(client, session("tied", "2026-10-15T17:30:00.000900+05:30"))

  assert.deepEqual(ids(readPages(client)), ["tied", "newer", "older"])
})

test("a pending stale list cannot erase a newly cached chat", async () => {
  const client = queryClient()
  const oldPage: SessionPage = {
    items: [session("old")],
    next_before_cursor: null,
  }
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [oldPage],
    pageParams: [null],
  })
  const stale = deferred<SessionPage>()
  const pendingRead = client.fetchInfiniteQuery({
    ...sessionPagesQueryOptions({ listSessionPages: () => stale.promise }),
    staleTime: 0,
  })

  cacheSession(client, session("new", "2026-10-16T12:00:00Z"))
  stale.resolve(oldPage)
  await pendingRead.catch((error: unknown) => {
    assert.equal(isCancelledError(error), true)
  })

  assert.deepEqual(ids(readPages(client)), ["new", "old"])
})

test("an overflowing page keeps every row while background reconciliation runs", async () => {
  const client = queryClient()
  const oldSessions = Array.from({ length: 40 }, (_, index) =>
    session(`old-${String(40 - index).padStart(2, "0")}`)
  )
  client.setQueryData<InfiniteData<SessionPage>>(serverKeys.sessionPages, {
    pages: [{ items: oldSessions, next_before_cursor: "opaque-cursor" }],
    pageParams: [null],
  })
  const refresh = deferred<SessionPage>()
  const refreshAfterUpdate = deferred<SessionPage>()
  let requests = 0
  const observer = new InfiniteQueryObserver(client, {
    ...sessionPagesQueryOptions({
      listSessionPages: () =>
        ++requests === 1 ? refresh.promise : refreshAfterUpdate.promise,
    }),
    staleTime: Infinity,
  })
  const snapshots: (string[] | undefined)[] = []
  const unsubscribe = observer.subscribe((result) => {
    snapshots.push(ids(result.data))
  })
  cleanups.push(unsubscribe)
  const created = session("new", "2026-10-16T12:00:00Z")

  cacheSession(client, created)

  assert.deepEqual(ids(readPages(client)), [
    "new",
    ...oldSessions.map((item) => item.id),
  ])
  assert.equal(observer.getCurrentResult().isFetching, true)
  assert.equal(
    snapshots.every((snapshot) => snapshot?.length === 41),
    true
  )
  const renamed = { ...created, title: "Renamed while reconciling" }
  cacheSession(client, renamed)
  assert.equal(observer.getCurrentResult().isFetching, true)
  assert.equal(requests, 2)
  const pendingRefresh = client
    .getQueryCache()
    .find({ queryKey: serverKeys.sessionPages, exact: true })?.promise
  const authoritativePage: SessionPage = {
    items: [renamed, ...oldSessions.slice(0, 39)],
    next_before_cursor: "authoritative-cursor",
  }
  refresh.resolve(authoritativePage)
  refreshAfterUpdate.resolve(authoritativePage)
  await pendingRefresh

  assert.deepEqual(readPages(client)?.pages, [authoritativePage])
})
