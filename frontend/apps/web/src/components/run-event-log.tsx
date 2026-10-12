import { useState } from "react"

import type { RuntimeRunEvent } from "@/lib/runtime-client"

const eventTimeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "short",
  timeStyle: "medium",
})

export function RunEventTimestamp({ createdAt }: { createdAt?: string }) {
  const timestamp = createdAt ? Date.parse(createdAt) : Number.NaN
  if (!Number.isFinite(timestamp)) {
    return <span className="run-event-time">Time unavailable</span>
  }

  return (
    <time className="run-event-time" dateTime={createdAt} title={createdAt}>
      {eventTimeFormat.format(timestamp)}
    </time>
  )
}

function RunEventLogEntry({ event }: { event: RuntimeRunEvent }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <li className="run-event-log-entry" data-log-sequence={event.sequence}>
      <details
        open={expanded}
        onToggle={(toggle) => setExpanded(toggle.currentTarget.open)}
      >
        <summary aria-label={`Event ${event.sequence}: ${event.eventType}`}>
          <span className="run-event-sequence">#{event.sequence}</span>{" "}
          <span className="run-event-type">{event.eventType}</span>
          <RunEventTimestamp createdAt={event.createdAt} />
        </summary>
        {expanded ? (
          <pre
            className="run-event-payload"
            aria-label={`Event ${event.sequence} details`}
            tabIndex={0}
          >
            {JSON.stringify(event, null, 2)}
          </pre>
        ) : null}
      </details>
    </li>
  )
}

export function RunEventLog({ events }: { events: RuntimeRunEvent[] }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <details
      className="run-event-log"
      open={expanded}
      onToggle={(toggle) => setExpanded(toggle.currentTarget.open)}
    >
      <summary className="run-event-log-toggle">
        Event log ({events.length})
      </summary>
      {expanded ? (
        <ol className="run-event-log-list" aria-label="Complete run event log">
          {events.map((event) => (
            <RunEventLogEntry
              key={`${event.runId}:${event.sequence}`}
              event={event}
            />
          ))}
        </ol>
      ) : null}
    </details>
  )
}
