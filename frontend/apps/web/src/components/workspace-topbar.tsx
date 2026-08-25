import {
  LayoutGrid,
  MessageSquare,
  Moon,
  Settings,
  Sun,
} from "lucide-react"
import type { MouseEvent } from "react"

import { useTheme } from "@/components/theme-provider"
import type { WorkspaceView } from "@/lib/app-types"

type ViewTransitionDocument = Document & {
  startViewTransition?: (callback: () => void) => { finished: Promise<void> }
}

type WorkspaceTopbarProps = {
  activeView: WorkspaceView
  onNavigate: (view: WorkspaceView) => void
}

export function WorkspaceTopbar({ activeView, onNavigate }: WorkspaceTopbarProps) {
  const { resolvedTheme, setTheme } = useTheme()

  const handleThemeToggle = (event: MouseEvent<HTMLButtonElement>) => {
    const nextTheme = resolvedTheme === "dark" ? "light" : "dark"
    const root = document.documentElement
    const buttonBounds = event.currentTarget.getBoundingClientRect()
    const rippleX = buttonBounds.left + buttonBounds.width / 2
    const rippleY = buttonBounds.top + buttonBounds.height / 2
    const rippleRadius = Math.hypot(
      Math.max(rippleX, window.innerWidth - rippleX),
      Math.max(rippleY, window.innerHeight - rippleY),
    )

    root.style.setProperty("--theme-ripple-x", `${rippleX}px`)
    root.style.setProperty("--theme-ripple-y", `${rippleY}px`)
    root.style.setProperty("--theme-ripple-radius", `${rippleRadius}px`)

    const viewTransitionDocument = document as ViewTransitionDocument
    if (viewTransitionDocument.startViewTransition) {
      const transition = viewTransitionDocument.startViewTransition(() => {
        setTheme(nextTheme)
      })

      const clearRippleCoordinates = () => {
        root.style.removeProperty("--theme-ripple-x")
        root.style.removeProperty("--theme-ripple-y")
        root.style.removeProperty("--theme-ripple-radius")
      }

      transition.finished.then(clearRippleCoordinates, clearRippleCoordinates)
      return
    }

    setTheme(nextTheme)
    root.classList.add("theme-ripple-fallback")
    window.setTimeout(() => {
      root.classList.remove("theme-ripple-fallback")
      root.style.removeProperty("--theme-ripple-x")
      root.style.removeProperty("--theme-ripple-y")
      root.style.removeProperty("--theme-ripple-radius")
    }, 620)
  }

  return (
    <div className="workspace-topbar">
      <div className="topbar-spacer" />
      <div className="topbar-actions">
        <button className="topbar-icon" aria-label="Layout"><LayoutGrid size={15} /></button>
        <button className="topbar-icon" aria-label="Messages"><MessageSquare size={15} /></button>
        <button
          className={`topbar-icon ${activeView === "Settings" ? "is-active" : ""}`}
          aria-label="Settings"
          aria-pressed={activeView === "Settings"}
          onClick={() => onNavigate("Settings")}
        >
          <Settings size={15} />
        </button>
        <button
          className="topbar-icon theme-toggle"
          aria-label={`Switch to ${resolvedTheme === "dark" ? "light" : "dark"} theme`}
          title={`Switch to ${resolvedTheme === "dark" ? "light" : "dark"} theme`}
          onClick={handleThemeToggle}
        >
          {resolvedTheme === "dark" ? <Sun size={15} strokeWidth={1.7} /> : <Moon size={15} strokeWidth={1.7} />}
        </button>
      </div>
    </div>
  )
}
