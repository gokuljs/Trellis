import {
  CalendarClock,
  FileText,
  GitBranch,
  Search,
} from "lucide-react"

import type { WorkspaceView } from "@/lib/app-types"

type WelcomePanelProps = {
  activeView: WorkspaceView
  activeSessionTitle: string | null
}

const capabilityItems = [
  { label: "Trace a codebase", icon: GitBranch },
  { label: "Search the web", icon: Search },
  { label: "Make artifacts", icon: FileText },
  { label: "Schedule work", icon: CalendarClock },
]

function CapabilityGrid() {
  return (
    <div className="capability-grid">
      {capabilityItems.map(({ label, icon: Icon }) => (
        <div key={label}><Icon size={16} /><span>{label}</span></div>
      ))}
    </div>
  )
}

export function WelcomePanel({ activeView, activeSessionTitle }: WelcomePanelProps) {
  if (activeView === "Capabilities") {
    return (
      <div className="utility-panel">
        <div className="utility-kicker">AGENT CAPABILITIES</div>
        <h1>Build with an agent that can follow the thread.</h1>
        <p>Research, code, inspect files, and keep a working context across every session.</p>
        <CapabilityGrid />
      </div>
    )
  }

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
    <div className="welcome-panel">
      <div className="welcome-kicker">A workspace for ideas in motion</div>
      <div className="wordmark" aria-label="Trellis">Trellis</div>
      <p className="welcome-copy">Bring a question, a file, or a rough idea. Trellis helps you find the next clear step.</p>
    </div>
  )
}
