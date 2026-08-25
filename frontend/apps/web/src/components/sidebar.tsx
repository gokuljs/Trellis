import { PanelLeft, SlidersHorizontal } from "lucide-react"

import { NAVIGATION_ITEMS } from "@/lib/app-data"
import type { Session, WorkspaceView } from "@/lib/app-types"

type SidebarProps = {
  activeView: WorkspaceView
  sessions: Session[]
  onNavigate: (view: WorkspaceView) => void
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  sidebarCollapsed: boolean
  onToggleSidebar: () => void
}

type PrimaryNavigationProps = Pick<SidebarProps, "activeView" | "onNavigate">

function PrimaryNavigation({ activeView, onNavigate }: PrimaryNavigationProps) {
  return (
    <nav className="primary-nav" aria-label="Primary">
      {NAVIGATION_ITEMS.map(({ label, icon: Icon }) => (
        <button
          className={`nav-item ${activeView === label ? "is-active" : ""}`}
          key={label}
          aria-current={activeView === label ? "page" : undefined}
          onClick={() => onNavigate(label)}
        >
          <Icon size={15} strokeWidth={1.6} aria-hidden="true" />
          <span>{label}</span>
          {label === "New session" ? (
            <span className="shortcut-hint">⌘ N</span>
          ) : null}
        </button>
      ))}
    </nav>
  )
}

function formatSessionDate(value: string) {
  const date = new Date(value)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return "today"
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(date)
}

function SessionList({
  sessions,
  activeSessionId,
  onSelectSession,
}: Pick<SidebarProps, "sessions" | "activeSessionId" | "onSelectSession">) {
  const groups = Object.entries(
    sessions.reduce<Record<string, Session[]>>((current, session) => {
      const month = new Intl.DateTimeFormat(undefined, {
        month: "long",
        year: "numeric",
      })
        .format(new Date(session.updated_at))
        .toUpperCase()
      current[month] = [...(current[month] ?? []), session]
      return current
    }, {})
  )

  return (
    <section className="sessions-section">
      <div className="section-heading">
        <span>
          <span className="section-mark">◆</span> SESSIONS
        </span>
        <SlidersHorizontal size={12} strokeWidth={1.5} />
      </div>

      {groups.map(([month, items]) => (
        <div className="session-group" key={month}>
          <div className="month-label">{month}</div>
          {items.map((session) => {
            const isSelected = session.id === activeSessionId
            return (
              <button
                className={`session-row ${isSelected ? "selected" : ""}`}
                key={session.id}
                aria-label={session.title}
                aria-current={isSelected ? "page" : undefined}
                onClick={() => onSelectSession(session.id)}
              >
                <span className="session-dot">•</span>
                <span className="session-title">{session.title}</span>
                <span className="session-date">
                  {formatSessionDate(session.updated_at)}
                </span>
              </button>
            )
          })}
        </div>
      ))}
    </section>
  )
}

export function Sidebar({
  activeView,
  sessions,
  onNavigate,
  activeSessionId,
  onSelectSession,
  sidebarCollapsed,
  onToggleSidebar,
}: SidebarProps) {
  return (
    <aside className="app-sidebar">
      <div className="sidebar-topbar">
        <span className="sidebar-wordmark" aria-label="Trellis">
          Trellis
        </span>
        <div className="sidebar-topbar-spacer" />
        <button
          className="icon-button"
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-pressed={sidebarCollapsed}
          onClick={onToggleSidebar}
        >
          <PanelLeft size={14} strokeWidth={1.7} aria-hidden="true" />
        </button>
      </div>

      <div className="sidebar-scroll">
        <PrimaryNavigation activeView={activeView} onNavigate={onNavigate} />

        <SessionList
          sessions={sessions}
          activeSessionId={activeSessionId}
          onSelectSession={onSelectSession}
        />
      </div>
    </aside>
  )
}
