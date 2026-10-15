import { useEffect, useId, useReducer, type CSSProperties } from "react"

import type { ContextPreview } from "@/lib/app-types"
import {
  contextGaugeDetails,
  isGaugeOpen,
  listenForContextEscape,
  newGaugeDisclosure,
  reduceGaugeDisclosure,
} from "@/lib/context-gauge"
import "./context-gauge.css"

type ContextGaugeProps = {
  preview: ContextPreview | null
  modelName: string
  loading: boolean
  error: string | null
  compacting?: boolean
}

export function ContextGaugeDetails({
  preview,
  modelName,
  loading,
  error,
  compacting,
}: ContextGaugeProps) {
  const details = preview ? contextGaugeDetails(preview) : null
  const status =
    compacting || preview?.status === "compacting"
      ? "Compacting and saving context…"
      : preview?.status === "compaction_required"
        ? "Will compact automatically before the next answer."
        : preview?.status === "unavailable"
          ? "Context usage unavailable."
          : preview?.checkpoint_id
            ? "Compacted context saved."
            : "Ready"
  return (
    <>
      <p className="context-gauge-heading">Context usage</p>
      <p className="context-gauge-model">{modelName || "Selected model"}</p>
      {error ? (
        <p className="context-gauge-status">{error}</p>
      ) : loading && !compacting ? (
        <p className="context-gauge-status">Updating context estimate…</p>
      ) : !preview && !compacting ? (
        <p className="context-gauge-status">Context usage is unavailable.</p>
      ) : (
        <>
          {details ? (
            <dl className="context-gauge-details">
              <div>
                <dt>Active context</dt>
                <dd>{details.usage}</dd>
              </div>
              <div>
                <dt>Model window used</dt>
                <dd>{details.percentage}</dd>
              </div>
              <div>
                <dt>Output reserved</dt>
                <dd>{details.outputReserve}</dd>
              </div>
              <div>
                <dt>Auto compaction at</dt>
                <dd>{details.threshold}</dd>
              </div>
            </dl>
          ) : null}
          <p className="context-gauge-status" role="status">
            {status}
          </p>
          {preview?.status !== "unavailable" && details ? (
            <p className="context-gauge-note">
              {preview?.count_method === "provider"
                ? "Provider count for this model call."
                : "Estimated input includes instructions, tools and the current conversation."}
            </p>
          ) : null}
        </>
      )}
    </>
  )
}

export function ContextGauge(props: ContextGaugeProps) {
  const detailsId = useId()
  const [disclosure, dispatch] = useReducer(
    reduceGaugeDisclosure,
    undefined,
    newGaugeDisclosure
  )
  const open = isGaugeOpen(disclosure)
  useEffect(() => {
    if (!open) return
    return listenForContextEscape(document, () => dispatch("escape"))
  }, [open])
  const percentage = props.preview?.percentage
  const style = {
    "--context-percentage": `${Math.min(100, Math.max(0, percentage ?? 0))}%`,
  } as CSSProperties
  return (
    <div
      className="context-gauge"
      onMouseEnter={() => dispatch("hover")}
      onMouseLeave={() => dispatch("leave")}
    >
      <button
        type="button"
        className="context-gauge-trigger"
        aria-label="Show context usage"
        aria-expanded={open}
        aria-controls={detailsId}
        aria-describedby={open ? detailsId : undefined}
        aria-busy={props.loading || props.compacting}
        onFocus={() => dispatch("focus")}
        onBlur={() => dispatch("blur")}
        onClick={() => dispatch("tap")}
      >
        <span
          className={`context-gauge-ring${percentage === null || percentage === undefined ? "is-unavailable" : ""}`}
          style={style}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div className="context-gauge-popover" role="tooltip" id={detailsId}>
          <ContextGaugeDetails {...props} />
        </div>
      ) : null}
    </div>
  )
}
