import {
  ArrowLeftRight,
  Check,
  MoreHorizontal,
  PanelLeft,
  SlidersHorizontal,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { NAVIGATION_ITEMS } from "@/lib/app-data"
import type { Session, WorkspaceView } from "@/lib/app-types"

type SidebarProps = {
  activeView: WorkspaceView
  sessions: Session[]
  onNavigate: (view: WorkspaceView) => void
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
          {label === "New session" ? <span className="shortcut-hint">⌘ N</span> : null}
        </button>
      ))}
    </nav>
  )
}

function SessionList({ sessions, onNavigate }: Pick<SidebarProps, "sessions" | "onNavigate">) {
  const groups = [
    { month: "", items: sessions.slice(0, 1) },
    { month: "JULY", items: sessions.slice(1, 5) },
    { month: "JUNE", items: sessions.slice(5) },
  ].filter((group) => group.items.length > 0)

  return (
    <section className="sessions-section">
      <div className="section-heading">
        <span><span className="section-mark">◆</span> SESSIONS</span>
        <SlidersHorizontal size={12} strokeWidth={1.5} />
      </div>

      {groups.map((group) => (
        <div className="session-group" key={group.month || "recent"}>
          {group.month ? <div className="month-label">{group.month}</div> : null}
          {group.items.map((session) => (
            <button
              className={`session-row ${session.active ? "selected" : ""}`}
              key={session.title}
              aria-current={session.active ? "page" : undefined}
              onClick={() => onNavigate("session")}
            >
              <span className="session-dot">•</span>
              <span className="session-title">{session.title}</span>
              <span className="session-date">{session.date}</span>
              {session.title === "Resume File Location" ? (
                <MoreHorizontal className="session-more" size={13} strokeWidth={1.8} aria-hidden="true" />
              ) : null}
            </button>
          ))}
        </div>
      ))}
    </section>
  )
}

export function Sidebar({ activeView, sessions, onNavigate, sidebarCollapsed, onToggleSidebar }: SidebarProps) {
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false)
  const workspaceSwitcherRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!workspaceMenuOpen) return undefined

    const handlePointerDown = (event: PointerEvent) => {
      if (!workspaceSwitcherRef.current?.contains(event.target as Node)) {
        setWorkspaceMenuOpen(false)
      }
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setWorkspaceMenuOpen(false)
      }
    }

    document.addEventListener("pointerdown", handlePointerDown)
    document.addEventListener("keydown", handleKeyDown)

    return () => {
      document.removeEventListener("pointerdown", handlePointerDown)
      document.removeEventListener("keydown", handleKeyDown)
    }
  }, [workspaceMenuOpen])

  return (
    <aside className="app-sidebar">
      <div className="sidebar-topbar">
        <span className="sidebar-wordmark" aria-label="Trellis">Trellis</span>
        <div className="sidebar-topbar-spacer" />
        <button
          className="icon-button"
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-pressed={sidebarCollapsed}
          onClick={onToggleSidebar}
        >
          <PanelLeft size={14} strokeWidth={1.7} aria-hidden="true" />
        </button>
        <div className="workspace-switcher" ref={workspaceSwitcherRef}>
          <button
            className={`icon-button ${workspaceMenuOpen ? "is-active" : ""}`}
            aria-label="Switch workspace"
            aria-haspopup="menu"
            aria-expanded={workspaceMenuOpen}
            onClick={() => setWorkspaceMenuOpen((open) => !open)}
          >
            <ArrowLeftRight size={14} strokeWidth={1.7} aria-hidden="true" />
          </button>
          {workspaceMenuOpen ? (
            <div className="workspace-menu" role="menu" aria-label="Workspaces">
              <div className="workspace-menu-label">WORKSPACE</div>
              <button className="workspace-option is-current" role="menuitem" onClick={() => setWorkspaceMenuOpen(false)}>
                <span className="workspace-option-mark">◆</span>
                <span className="workspace-option-name">Trellis</span>
                <Check size={13} strokeWidth={2} aria-hidden="true" />
              </button>
              <p className="workspace-menu-note">Trellis is your current workspace.</p>
            </div>
          ) : null}
        </div>
      </div>

      <div className="sidebar-scroll">
        <PrimaryNavigation activeView={activeView} onNavigate={onNavigate} />

        <SessionList sessions={sessions} onNavigate={onNavigate} />
      </div>
    </aside>
  )
}
