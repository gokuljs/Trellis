import {
  ArrowLeftRight,
  CircleHelp,
  MoreHorizontal,
  PanelLeft,
  Pin,
  Plus,
  Search,
  SlidersHorizontal,
  VolumeX,
} from "lucide-react"

import { NAVIGATION_ITEMS } from "@/lib/app-data"
import type { Session, WorkspaceView } from "@/lib/app-types"

type SidebarProps = {
  activeView: WorkspaceView
  sessions: Session[]
  onNavigate: (view: WorkspaceView) => void
  onClose: () => void
}

type PrimaryNavigationProps = Pick<SidebarProps, "activeView" | "onNavigate">

function PrimaryNavigation({ activeView, onNavigate }: PrimaryNavigationProps) {
  return (
    <nav className="primary-nav" aria-label="Primary">
      {NAVIGATION_ITEMS.map(({ label, icon: Icon }) => (
        <button
          className={`nav-item ${activeView === label ? "is-active" : ""}`}
          key={label}
          onClick={() => onNavigate(label)}
        >
          <Icon size={15} strokeWidth={1.6} />
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
              onClick={() => onNavigate("session")}
            >
              <span className="session-dot">•</span>
              <span className="session-title">{session.title}</span>
              <span className="session-date">{session.date}</span>
              {session.title === "Resume File Location" ? (
                <MoreHorizontal className="session-more" size={13} strokeWidth={1.8} />
              ) : null}
            </button>
          ))}
        </div>
      ))}
    </section>
  )
}

function SidebarFooter({ onNewSession }: { onNewSession: () => void }) {
  return (
    <div className="sidebar-footer">
      <div className="footer-actions">
        <button className="round-status" aria-label="Status"><span>◈</span></button>
        <button className="footer-icon" aria-label="Add" onClick={onNewSession}><Plus size={13} /></button>
        <button className="footer-icon" aria-label="Help"><CircleHelp size={14} /></button>
        <div className="footer-spacer" />
        <button className="footer-icon" aria-label="More"><MoreHorizontal size={15} /></button>
        <button className="footer-icon" aria-label="Power"><span className="power-mark">◉</span></button>
      </div>
    </div>
  )
}

export function Sidebar({ activeView, sessions, onNavigate, onClose }: SidebarProps) {
  return (
    <aside className="app-sidebar">
      <div className="sidebar-topbar">
        <span className="sidebar-wordmark" aria-label="Trellis">Trellis</span>
        <div className="sidebar-topbar-spacer" />
        <button className="icon-button" aria-label="Toggle sidebar" onClick={onClose}>
          <PanelLeft size={14} strokeWidth={1.7} />
        </button>
        <button className="icon-button" aria-label="Switch workspace">
          <ArrowLeftRight size={14} strokeWidth={1.7} />
        </button>
      </div>

      <div className="sidebar-scroll">
        <PrimaryNavigation activeView={activeView} onNavigate={onNavigate} />

        <div className="search-field">
          <Search size={14} strokeWidth={1.6} />
          <span>Search sessions...</span>
        </div>

        <section className="pinned-section">
          <div className="section-heading">
            <span><Pin size={11} fill="currentColor" /> PINNED</span>
          </div>
          <button className="hint-row">
            <VolumeX size={13} strokeWidth={1.5} />
            <span>Shift-click a chat to pin</span>
          </button>
        </section>

        <SessionList sessions={sessions} onNavigate={onNavigate} />
      </div>

      <SidebarFooter onNewSession={() => onNavigate("New session")} />
    </aside>
  )
}
