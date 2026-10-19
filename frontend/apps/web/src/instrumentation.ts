import { parseTelemetryConfig } from "./lib/telemetry-policy"
import { initializeTelemetry } from "./lib/telemetry"

initializeTelemetry(
  parseTelemetryConfig({ ...import.meta.env, origin: window.location.origin })
)
