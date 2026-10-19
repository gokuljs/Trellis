import "./instrumentation"

import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "@trellis/ui/globals.css"
import { App } from "./App.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { RootErrorBoundary } from "@/components/root-error-boundary"
import { telemetryRootOptions } from "@/lib/telemetry"

const app = (
  <RootErrorBoundary>
    <ThemeProvider defaultTheme="dark" disableTransitionOnChange={false}>
      <App />
    </ThemeProvider>
  </RootErrorBoundary>
)

// metal-fx's shared WebGL renderer freezes when StrictMode replays effects in dev.
createRoot(document.getElementById("root")!, telemetryRootOptions()).render(
  import.meta.env.DEV ? app : <StrictMode>{app}</StrictMode>
)
