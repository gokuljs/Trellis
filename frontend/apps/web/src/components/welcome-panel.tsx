import type { WorkspaceView } from "@/lib/app-types"
import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import "./welcome-panel.css"

type WelcomePanelProps = {
  activeView: WorkspaceView
  activeSessionTitle: string | null
}

export function WelcomePanel({
  activeView,
  activeSessionTitle,
}: WelcomePanelProps) {
  if (activeView === "session") {
    return (
      <div className="utility-panel compact">
        <div className="utility-kicker">SESSION</div>
        <h1>{activeSessionTitle ?? "New session"}</h1>
        <p>Trellis is ready for the next piece of work.</p>
      </div>
    )
  }

  return (
    <div className="welcome-scene">
      <p className="welcome-scene__copy">What would you like to work on?</p>
      <TrellisMark className="welcome-dither" size={24} animated />
    </div>
  )
}
