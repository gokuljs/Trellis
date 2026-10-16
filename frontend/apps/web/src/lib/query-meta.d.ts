import type { TelemetryOperation } from "./telemetry-policy"

interface TelemetryMeta extends Record<string, unknown> {
  telemetryOperation?: TelemetryOperation
  telemetryOwner?: "cache" | "caller"
}

declare module "@tanstack/react-query" {
  interface Register {
    queryMeta: TelemetryMeta
    mutationMeta: TelemetryMeta
  }
}
