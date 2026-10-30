import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { MessageMarkdown } from "../src/components/message-markdown"
import { WebEvidenceScope } from "../src/components/web-sources"
import { getAuthController } from "../src/lib/auth-controller"
import { threadMessages } from "../src/lib/assistant-outputs"
import { groupRunEvents } from "../src/lib/run-event-groups"
import { describeActivity } from "../src/lib/activity-status"
import {
  citationTokens,
  safePublicSourceUrl,
  transformCitationTree,
} from "../src/lib/web-evidence"
import {
  discardWebEvidence,
  webCitationsQueryOptions,
  webEvidenceKeys,
  webSourceLinesQueryOptions,
} from "../src/lib/web-evidence-queries"
import type { AssistantOutput, Message } from "../src/lib/app-types"
import type { RuntimeRunEvent } from "../src/lib/runtime-client"

const sourceId = "ABCDEFAB-1111-4111-A111-111111111111"
const marker = `[[source:${sourceId}:L1-L2]]`
const cleanups: (() => void)[] = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
})

function parsedReferences(content: string) {
  let references: ReturnType<typeof transformCitationTree> = []
  const collect = () => (tree: Parameters<typeof transformCitationTree>[0]) => {
    references = transformCitationTree(tree, content)
  }
  renderToStaticMarkup(
    createElement(Markdown, {
      remarkPlugins: [remarkGfm, collect],
      children: content,
    })
  )
  return references
}

test("citation parsing matches only raw Markdown text, canonicalizes UUIDs and deduplicates ranges", () => {
  assert.deepEqual(
    parsedReferences(`First ${marker}, again ${marker} and **${marker}**.`),
    [{ source_id: sourceId.toLowerCase(), start_line: 1, end_line: 2 }]
  )
  for (const content of [
    `\`${marker}\``,
    `\`\`\`text\n${marker}\n\`\`\``,
    `[${marker}](https://example.test)`,
    `[text](https://example.test/${marker})`,
    `![${marker}](https://example.test/image)`,
    `[label][ref]\n\n[ref]: https://example.test/${marker}`,
    `\\${marker}`,
    marker.replace("[[", "&#91;&#91;"),
    marker.replace(sourceId, `**${sourceId}**`),
  ])
    assert.deepEqual(parsedReferences(content), [], content)
  for (const range of [
    "L01-L002",
    "L0-L2",
    "L2-L1",
    "L1-L201",
    "L262145-L262146",
  ]) {
    const token = citationTokens(`[[source:${sourceId}:${range}]]`)[0]
    assert.ok(typeof token === "object" && token.reference === null)
  }
  assert.equal(
    parsedReferences(`[[source:${sourceId}:L262145-L262145]]`).length,
    1
  )
})

test("source navigation only accepts public HTTP URLs without secrets or credentials", () => {
  assert.equal(
    safePublicSourceUrl("https://example.test/article"),
    "https://example.test/article"
  )
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,test",
    "https://user:pass@example.test/",
    "https://localhost/",
    "https://service.local/",
    "https://intranet/",
    "http://127.1/",
    "http://2130706433/",
    "http://192.168.1.3/",
    "http://[::1]/",
    "https://example.test/?token=[REDACTED]",
    "https://example.test/?token=%5BREDACTED%5D",
  ])
    assert.equal(safePublicSourceUrl(url), null, url)
})

function markdown(
  content: string,
  state: "streaming" | "final" | "unverified" = "unverified"
) {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity } },
  })
  cleanups.push(() => client.clear())
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(MessageMarkdown, { content, citationState: state })
    )
  )
}

test("streams show pending citations and terminal partial output shows unavailable citations", () => {
  assert.ok(
    markdown(`Waiting [[source:${sourceId}:L1-`, "streaming").includes(
      "Citation pending"
    )
  )
  const partial = markdown(`Waiting [[source:${sourceId}:L1-`, "unverified")
  assert.ok(partial.includes("Citation unavailable"))
  assert.ok(!partial.includes("Citation pending"))
  assert.ok(markdown(`\`${marker}\``, "streaming").includes("[[source:"))
})

test("Markdown never creates automatic image requests or executes raw HTML", () => {
  const html = markdown(
    '![tracking](https://images.example.test/pixel.png)\n\n<img src="https://images.example.test/html.png"><script>alert(1)</script>'
  )
  assert.ok(
    !html.includes("<img") &&
      !html.includes("<script") &&
      !html.includes(" src=")
  )
  assert.ok(html.includes('href="https://images.example.test/pixel.png"'))
  assert.ok(html.includes("tracking (open image)"))
})

test("only exact authenticated final bindings become source controls", () => {
  const controller = getAuthController()
  const previous = controller.getSnapshot
  const auth = previous()
  controller.getSnapshot = () => ({
    ...auth,
    status: "signed-in",
    user: { id: "owner" } as NonNullable<typeof auth.user>,
  })
  cleanups.push(() => {
    controller.getSnapshot = previous
  })
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity } },
  })
  cleanups.push(() => client.clear())
  client.setQueryData(webEvidenceKeys.citations("owner", "chat", "call"), {
    items: [
      {
        model_call_id: "call",
        source_id: sourceId.toLowerCase(),
        start_line: 1,
        end_line: 2,
        citation_index: 1,
      },
    ],
  })
  const content = `${marker} and [[source:${sourceId}:L3-L4]]`
  const html = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(WebEvidenceScope, {
        sessionId: "chat",
        active: false,
        children: createElement(MessageMarkdown, {
          content,
          modelCallId: "call",
          citationState: "final",
        }),
      })
    )
  )
  assert.equal(
    (html.match(/aria-label="Source 1, lines 1 to 2"/g) ?? []).length,
    1
  )
  assert.ok(html.includes("Citation unavailable"))
  assert.equal(
    client
      .getQueryCache()
      .findAll({ queryKey: ["web-evidence", "owner", "chat"] })
      .filter((query) => query.state.fetchStatus === "fetching").length,
    0
  )
})

test("promoted outputs retain final model-call identity and message timeline keys", () => {
  const message: Message = {
    id: "message",
    turn_id: "turn",
    role: "assistant",
    content: marker,
    provider: null,
    model: null,
    created_at: "now",
  }
  const output: AssistantOutput = {
    id: "output",
    turn_id: "turn",
    run_id: "run",
    model_call_id: "call",
    step_index: 1,
    content: marker,
    status: "complete",
    stop_reason: null,
    promoted_message_id: "message",
    created_at: "now",
  }
  const result = threadMessages([message], [output, output], {}, [
    { kind: "message", id: "message" },
  ])
  assert.equal(result.length, 1)
  assert.equal(result[0]?.modelCallId, "call")
  assert.equal(result[0]?.finalOutput, true)
  assert.equal(result[0]?.output, null)
  assert.equal(result[0]?.id, "message")
})

test("source caches are scoped by account and chat, and clearing aborts outstanding reads", async () => {
  const client = new QueryClient()
  cleanups.push(() => client.clear())
  let signal: AbortSignal | undefined
  const options = webSourceLinesQueryOptions(
    "owner",
    "chat",
    "source",
    1,
    200,
    0,
    {
      readWebSource: async (_session, _source, _start, _count, request) => {
        signal = request?.signal
        return new Promise(() => undefined)
      },
    }
  )
  const observer = new QueryObserver(client, options)
  const unsubscribe = observer.subscribe(() => undefined)
  cleanups.push(unsubscribe)
  await Promise.resolve()
  assert.equal(signal?.aborted, false)
  await discardWebEvidence(client, "owner", "chat")
  assert.equal(signal?.aborted, true)
  assert.equal(
    client
      .getQueryCache()
      .findAll({ queryKey: webEvidenceKeys.scope("owner", "chat") }).length,
    0
  )
  assert.notDeepEqual(
    webEvidenceKeys.citations("owner", "chat", "call"),
    webEvidenceKeys.citations("other", "chat", "call")
  )
  assert.notDeepEqual(
    webEvidenceKeys.citations("owner", "chat", "call"),
    webEvidenceKeys.citations("owner", "other", "call")
  )
  let received: AbortSignal | undefined
  const citations = webCitationsQueryOptions("owner", "chat", "call", 0, {
    readWebCitations: async (_chat, _call, request) => {
      received = request?.signal
      return { items: [] }
    },
  })
  await client.fetchQuery(citations)
  assert.ok(received instanceof AbortSignal)
})

test("web progress remains attached to the requested tool in replay order", () => {
  const event = (
    eventType: string,
    sequence: number,
    data: Record<string, unknown>
  ): RuntimeRunEvent => ({
    runId: "run",
    sequence,
    eventType,
    eventVersion: 1,
    data,
  })
  const groups = groupRunEvents([
    event("run.phase", 3, {
      tool_call_id: "tool",
      phase: "web_crawl",
      status: "page_completed",
      pages_completed: 1,
      max_pages: 2,
    }),
    event("tool.call", 1, { tool_call_id: "tool", name: "web_crawl" }),
    event("tool.result", 4, {
      tool_call_id: "tool",
      name: "web_crawl",
      status: "completed",
    }),
  ])
  assert.equal(groups.length, 1)
  assert.equal(groups[0]?.title, "Tool · web_crawl")
  assert.deepEqual(
    groups[0]?.events.map((event) => event.sequence),
    [1, 3, 4]
  )
})

test("collapsed run activity names the active web collection phase", () => {
  for (const [phase, label] of [
    ["web_search", "Searching the web"],
    ["web_read", "Reading a page"],
    ["web_crawl", "Collecting site pages"],
  ]) {
    const event: RuntimeRunEvent = {
      runId: "run",
      sequence: 1,
      eventType: "run.phase",
      eventVersion: 1,
      data: { phase, tool_call_id: "tool", status: "started" },
    }
    assert.equal(describeActivity([event], true, 0).label, label)
  }
})
