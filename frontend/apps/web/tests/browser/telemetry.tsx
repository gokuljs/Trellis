import * as Sentry from "@sentry/react"
import { useState } from "react"
import { createRoot, hydrateRoot } from "react-dom/client"

import "@trellis/ui/globals.css"
import { RootErrorBoundary } from "../../src/components/root-error-boundary"
import { parseTelemetryConfig } from "../../src/lib/telemetry-policy"
import {
  createTelemetryOptions,
  telemetryRootOptions,
} from "../../src/lib/telemetry"

type Envelope = Parameters<
  ReturnType<NonNullable<Sentry.BrowserOptions["transport"]>>["send"]
>[0]
const sentinel = "trellis-private-fixture-token"
const envelopes: Envelope[] = []
const requests: {
  target: "api" | "external"
  headers: Record<string, string>
}[] = []
const triggered = new Set<string>()
let initialHeaders: ReturnType<typeof Sentry.getTraceData> = {}
const nativeFetch = window.fetch.bind(window)

window.fetch = async (input, init) => {
  const url = new URL(
    input instanceof Request ? input.url : String(input),
    window.location.href
  )
  if (
    url.pathname === "/api/telemetry-fixture" ||
    url.hostname === "telemetry-external.invalid"
  ) {
    const headers = new Headers(
      input instanceof Request ? input.headers : init?.headers
    )
    requests.push({
      target: url.origin === window.location.origin ? "api" : "external",
      headers: Object.fromEntries(headers.entries()),
    })
    return new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  }
  return nativeFetch(input, init)
}

function renderEvidence() {
  const events = envelopes.flatMap((envelope) =>
    envelope[1].flatMap(([header, payload]) =>
      header.type === "event" ? [payload as Sentry.ErrorEvent] : []
    )
  )
  const spans = envelopes.flatMap((envelope) =>
    envelope[1].filter(([header]) => header.type === "span")
  )
  const encoded = JSON.stringify({ envelopes, requests, initialHeaders })
  const errorCount = events.filter((event) => event.level === "error").length
  const warningCount = events.filter(
    (event) => event.level === "warning"
  ).length
  const expectedErrors =
    Number(triggered.has("render")) + Number(triggered.has("uncaught"))
  const expectedWarnings = Number(triggered.has("recoverable"))
  const api = requests.find((request) => request.target === "api")
  const external = requests.find((request) => request.target === "external")
  document.getElementById("verification")!.textContent = JSON.stringify(
    {
      privacy: encoded.includes(sentinel)
        ? "FAIL: private fixture value escaped"
        : "PASS",
      captureCounts:
        errorCount === expectedErrors && warningCount === expectedWarnings
          ? "PASS"
          : "PENDING OR FAIL",
      errorCount,
      warningCount,
      spanEnvelopes: spans.length,
      inheritedBaggage: JSON.stringify(initialHeaders).includes(sentinel)
        ? "FAIL"
        : "PASS",
      apiPropagation: api
        ? api.headers["sentry-trace"] && api.headers.baggage
          ? "PASS"
          : "FAIL"
        : "Not triggered",
      externalPropagation: external
        ? !external.headers["sentry-trace"] && !external.headers.baggage
          ? "PASS"
          : "FAIL"
        : "Not triggered",
      initialHeaders,
      requestHeaders: requests,
    },
    null,
    2
  )
  document.getElementById("envelopes")!.textContent = JSON.stringify(
    envelopes,
    null,
    2
  )
}

const config = parseTelemetryConfig({
  MODE: "development",
  PROD: false,
  VITE_SENTRY_ENABLED: "true",
  VITE_SENTRY_DSN: "https://public@o123.ingest.sentry.io/456",
  VITE_SENTRY_TRACES_SAMPLE_RATE: "1",
  VITE_SENTRY_RELEASE: "telemetry-browser-fixture",
  origin: window.location.origin,
})
const client = Sentry.init({
  ...createTelemetryOptions(config),
  transport: () => ({
    send(envelope) {
      envelopes.push(envelope)
      queueMicrotask(renderEvidence)
      return Promise.resolve({ statusCode: 200 })
    },
    flush: () => Promise.resolve(true),
  }),
})!
initialHeaders = Sentry.getTraceData()

const caughtRoot = createRoot(
  document.getElementById("caught-root")!,
  telemetryRootOptions()
)
const uncaughtRoot = createRoot(
  document.getElementById("uncaught-root")!,
  telemetryRootOptions()
)

export function Failure(): never {
  throw new Error(`Render failure with ${sentinel}`)
}

async function flushEvidence() {
  await client.flush(1000)
  renderEvidence()
}

export function Controls() {
  const [clicked, setClicked] = useState<string[]>([])
  function trigger(action: string, run: () => void) {
    triggered.add(action)
    setClicked((current) => [...current, action])
    run()
    setTimeout(() => void flushEvidence(), 100)
  }
  return (
    <>
      <button
        disabled={clicked.includes("render")}
        onClick={() =>
          trigger("render", () =>
            caughtRoot.render(
              <RootErrorBoundary>
                <Failure />
              </RootErrorBoundary>
            )
          )
        }
      >
        Trigger render failure
      </button>
      <button
        disabled={clicked.includes("uncaught")}
        onClick={() =>
          trigger("uncaught", () => uncaughtRoot.render(<Failure />))
        }
      >
        Trigger uncaught failure
      </button>
      <button
        disabled={clicked.includes("recoverable")}
        onClick={() =>
          trigger("recoverable", () => {
            hydrateRoot(
              document.getElementById("recoverable-root")!,
              <span>Recovered client fixture content</span>,
              telemetryRootOptions()
            )
          })
        }
      >
        Trigger recoverable warning
      </button>
      <button
        disabled={clicked.includes("api")}
        onClick={() =>
          trigger("api", () => {
            void Promise.all([
              fetch(`/api/telemetry-fixture?draft=${sentinel}`),
              fetch(
                `https://telemetry-external.invalid/api/fixture?token=${sentinel}`
              ),
            ]).then(flushEvidence)
          })
        }
      >
        Create API span
      </button>
    </>
  )
}

createRoot(document.getElementById("controls")!, telemetryRootOptions()).render(
  <Controls />
)
renderEvidence()
