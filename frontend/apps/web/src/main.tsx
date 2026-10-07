import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "@trellis/ui/globals.css"
import { App } from "./App.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"

const app = (
  <ThemeProvider defaultTheme="dark" disableTransitionOnChange={false}>
    <App />
  </ThemeProvider>
)

// metal-fx's shared WebGL renderer freezes when StrictMode replays effects in dev.
createRoot(document.getElementById("root")!).render(
  import.meta.env.DEV ? app : <StrictMode>{app}</StrictMode>
)
