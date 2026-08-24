import { useMemo, useState, type KeyboardEvent } from "react"
import {
  ArrowLeftRight,
  Boxes,
  CalendarClock,
  ChevronDown,
  CircleHelp,
  FileText,
  GitBranch,
  LayoutGrid,
  Menu,
  MessageSquare,
  Mic,
  MoreHorizontal,
  PanelLeft,
  Pin,
  Plus,
  Search,
  Send,
  Settings,
  SlidersHorizontal,
  VolumeX,
} from "lucide-react"

type NavItem = {
  label: string
  icon: typeof LayoutGrid
}

type Session = {
  title: string
  date: string
  active?: boolean
}

const navItems: NavItem[] = [
  { label: "New session", icon: Plus },
  { label: "Capabilities", icon: Boxes },
]

const initialSessions: Session[] = []

function Sidebar({
  activeNav,
  sessions,
  onNavigate,
  onClose,
}: {
  activeNav: string
  sessions: Session[]
  onNavigate: (label: string) => void
  onClose: () => void
}) {
  const groupedSessions = useMemo(
    () => [
      { month: "", items: sessions.slice(0, 1) },
      { month: "JULY", items: sessions.slice(1, 5) },
      { month: "JUNE", items: sessions.slice(5) },
    ],
    [sessions]
  )

  return (
    <aside className="app-sidebar">
      <div className="sidebar-topbar">
        <button className="icon-button" aria-label="Toggle sidebar" onClick={onClose}>
          <PanelLeft size={14} strokeWidth={1.7} />
        </button>
        <button className="icon-button" aria-label="Switch workspace">
          <ArrowLeftRight size={14} strokeWidth={1.7} />
        </button>
        <div className="sidebar-topbar-spacer" />
      </div>

      <div className="sidebar-scroll">
        <div className="eyebrow-row">
          <span>SESSIONS</span>
        </div>

        <nav className="primary-nav" aria-label="Primary">
          {navItems.map(({ label, icon: Icon }) => (
            <button
              className={`nav-item ${activeNav === label ? "is-active" : ""}`}
              key={label}
              onClick={() => onNavigate(label)}
            >
              <Icon size={15} strokeWidth={1.6} />
              <span>{label}</span>
              {label === "New session" && <span className="shortcut-hint">⌘ N</span>}
            </button>
          ))}
        </nav>

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

        <section className="sessions-section">
          <div className="section-heading">
            <span><span className="section-mark">◆</span> SESSIONS</span>
            <SlidersHorizontal size={12} strokeWidth={1.5} />
          </div>

          {groupedSessions.filter((group) => group.items.length > 0).map((group) => (
            <div className="session-group" key={group.month || "recent"}>
              {group.month && <div className="month-label">{group.month}</div>}
              {group.items.map((session) => (
                <button
                  className={`session-row ${session.active ? "selected" : ""}`}
                  key={session.title}
                  onClick={() => onNavigate("session")}
                >
                  <span className="session-dot">•</span>
                  <span className="session-title">{session.title}</span>
                  <span className="session-date">{session.date}</span>
                  {session.title === "Resume File Location" && (
                    <MoreHorizontal className="session-more" size={13} strokeWidth={1.8} />
                  )}
                </button>
              ))}
            </div>
          ))}
        </section>
      </div>

      <div className="sidebar-footer">
        <div className="footer-actions">
          <button className="round-status" aria-label="Status"><span>◈</span></button>
          <button className="footer-icon" aria-label="Add" onClick={() => onNavigate("New session")}><Plus size={13} /></button>
          <button className="footer-icon" aria-label="Help"><CircleHelp size={14} /></button>
          <div className="footer-spacer" />
          <button className="footer-icon" aria-label="More"><MoreHorizontal size={15} /></button>
          <button className="footer-icon" aria-label="Power"><span className="power-mark">◉</span></button>
        </div>
      </div>
    </aside>
  )
}

function WelcomePanel({
  activeNav,
  activeSessionTitle,
}: {
  activeNav: string
  activeSessionTitle: string | null
}) {
  if (activeNav === "Capabilities") {
    return (
      <div className="utility-panel">
        <div className="utility-kicker">AGENT CAPABILITIES</div>
        <h1>Build with an agent that can follow the thread.</h1>
        <p>Research, code, inspect files, and keep a working context across every session.</p>
        <div className="capability-grid">
          <div><GitBranch size={16} /><span>Trace a codebase</span></div>
          <div><Search size={16} /><span>Search the web</span></div>
          <div><FileText size={16} /><span>Make artifacts</span></div>
          <div><CalendarClock size={16} /><span>Schedule work</span></div>
        </div>
      </div>
    )
  }

  if (activeNav !== "New session") {
    return (
      <div className="utility-panel compact">
        <div className="utility-kicker">{activeNav.toUpperCase()}</div>
        <h1>{activeNav === "session" ? activeSessionTitle ?? "New session" : `Your ${activeNav.toLowerCase()} live here.`}</h1>
        <p>This workspace is ready for the next piece of work.</p>
      </div>
    )
  }

  return (
    <div className="welcome-panel">
      <div className="wordmark" aria-label="Name">NAME</div>
      <p className="welcome-copy">Drop a file path, a traceback, or a rough idea. I&apos;ll investigate, suggest next steps, and<br className="desktop-break" /> keep things reversible.</p>
    </div>
  )
}

export function App() {
  const [activeNav, setActiveNav] = useState("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [sessions, setSessions] = useState<Session[]>(initialSessions)
  const [activeSessionTitle, setActiveSessionTitle] = useState<string | null>(null)

  const startNewSession = () => {
    setComposerValue("")
    setActiveSessionTitle(null)
    setActiveNav("New session")
    setSidebarOpen(false)
  }

  const submitComposer = () => {
    const nextTitle = composerValue.trim()
    if (!nextTitle) return

    setSessions((current) => [
      { title: nextTitle, date: "now", active: true },
      ...current.map((session) => ({ ...session, active: false })),
    ])
    setComposerValue("")
    setActiveSessionTitle(nextTitle)
    setActiveNav("session")
  }

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      submitComposer()
    }
  }

  return (
    <main className="app-shell">
      <button className="mobile-menu" aria-label="Open navigation" onClick={() => setSidebarOpen(true)}>
        <Menu size={17} />
      </button>

      <div className={`sidebar-overlay ${sidebarOpen ? "visible" : ""}`} onClick={() => setSidebarOpen(false)} />
      <div className={`sidebar-container ${sidebarOpen ? "open" : ""}`}>
        <Sidebar
          activeNav={activeNav}
          sessions={sessions}
          onNavigate={(label) => {
            if (label === "New session") {
              startNewSession()
              return
            }

            setActiveNav(label)
            setSidebarOpen(false)
          }}
          onClose={() => setSidebarOpen(false)}
        />
      </div>

      <section className="workspace">
        <div className="workspace-topbar">
          <div className="topbar-spacer" />
          <div className="topbar-actions">
            <button className="topbar-icon" aria-label="Layout"><LayoutGrid size={15} /></button>
            <button className="topbar-icon" aria-label="Messages"><MessageSquare size={15} /></button>
            <button className="topbar-icon" aria-label="Settings"><Settings size={15} /></button>
            <button className="topbar-icon" aria-label="Theme"><span className="theme-chip" /></button>
          </div>
        </div>

        <div className="workspace-content">
          <WelcomePanel activeNav={activeNav} activeSessionTitle={activeSessionTitle} />
        </div>

        <div className="composer-wrap">
          <div className="composer-box">
            <button className="composer-add" aria-label="Attach"><Plus size={16} strokeWidth={1.6} /></button>
            <textarea
              aria-label="Message"
              value={composerValue}
              onChange={(event) => setComposerValue(event.target.value)}
              onKeyDown={handleComposerKeyDown}
              placeholder={activeNav === "New session" ? "What are we building?" : "Adjust or continue"}
              rows={1}
            />
            <div className="composer-tools">
              <span className="model-label">GPT-5.5 · Med <ChevronDown size={11} /></span>
              <button className="composer-tool" aria-label="Voice input"><Mic size={14} /></button>
              <button className="composer-tool" aria-label="Mute"><VolumeX size={14} /></button>
              <button className={`send-button ${composerValue.trim() ? "ready" : ""}`} aria-label="Send" onClick={submitComposer}>
                {composerValue.trim() ? <Send size={14} /> : <span className="voice-orb">◔</span>}
              </button>
            </div>
          </div>
          <div className="composer-footnote">Name can make mistakes. Check important info.</div>
        </div>
      </section>
    </main>
  )
}
