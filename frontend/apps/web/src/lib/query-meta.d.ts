import type { LogOperation } from "./logger"

interface LoggingMeta extends Record<string, unknown> {
  logOperation?: LogOperation
  logOwner?: "cache" | "caller"
}

declare module "@tanstack/react-query" {
  interface Register {
    queryMeta: LoggingMeta
    mutationMeta: LoggingMeta
  }
}
