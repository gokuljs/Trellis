import { useState } from "react"
import { Menu } from "lucide-react"

import { Composer } from "@/components/composer"
import { Sidebar } from "@/components/sidebar"
import { SettingsPage } from "@/components/settings-page"
import { WelcomePanel } from "@/components/welcome-panel"
import { WorkspaceTopbar } from "@/components/workspace-topbar"
import { INITIAL_SESSIONS } from "@/lib/app-data"
import type { Session, WorkspaceView } from "@/lib/app-types"

export function AppShell() {
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [sessions, setSessions] = useState<Session[]>(INITIAL_SESSIONS)
  const [activeSessionTitle, setActiveSessionTitle] = useState<string | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  const startNewSession = () => {
    setComposerValue("")
    setActiveSessionTitle(null)
    setActiveView("New session")
    setSidebarOpen(false)
    setSidebarCollapsed(false)
  }

  const handleNavigate = (view: WorkspaceView) => {
    if (view === "New session") {
      startNewSession()
      return
    }

    setActiveView(view)
    setSidebarOpen(false)
  }

  const toggleSidebar = () => {
    if (window.innerWidth <= 760) {
      setSidebarOpen(false)
      return
    }

    setSidebarCollapsed((collapsed) => !collapsed)
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
    setActiveView("session")
  }

  return (
    <main className="app-shell">
      <button
        className={`mobile-menu ${sidebarCollapsed ? "sidebar-restorer" : ""}`}
        aria-label="Open navigation"
        onClick={() => {
          if (window.innerWidth <= 760) {
            setSidebarOpen(true)
          } else {
            setSidebarCollapsed(false)
          }
        }}
      >
        <Menu size={17} aria-hidden="true" />
      </button>

      <button
        className={`sidebar-overlay ${sidebarOpen ? "visible" : ""}`}
        type="button"
        aria-label="Close navigation"
        onClick={() => setSidebarOpen(false)}
      />
      <div className={`sidebar-container ${sidebarOpen ? "open" : ""} ${sidebarCollapsed ? "collapsed" : ""}`}>
        <Sidebar
          activeView={activeView}
          sessions={sessions}
          onNavigate={handleNavigate}
          sidebarCollapsed={sidebarCollapsed}
          onToggleSidebar={toggleSidebar}
        />
      </div>

      <section className="workspace">
        <WorkspaceTopbar activeView={activeView} onNavigate={handleNavigate} />

        <div className={`workspace-content ${activeView === "Settings" ? "settings-content" : ""}`}>
          {activeView === "Settings" ? (
            <SettingsPage />
          ) : (
            <WelcomePanel activeView={activeView} activeSessionTitle={activeSessionTitle} />
          )}
        </div>

        {activeView !== "Settings" ? (
          <Composer
            value={composerValue}
            placeholder={activeView === "New session" ? "What are we building?" : "Adjust or continue"}
            onChange={setComposerValue}
            onSubmit={submitComposer}
          />
        ) : null}
      </section>
    </main>
  )
}
