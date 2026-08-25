import { Menu } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { ChatThread } from "@/components/chat-thread"
import { Composer } from "@/components/composer"
import { SettingsPage } from "@/components/settings-page"
import { Sidebar } from "@/components/sidebar"
import { WelcomePanel } from "@/components/welcome-panel"
import { WorkspaceTopbar } from "@/components/workspace-topbar"
import { ApiError, api } from "@/lib/api"
import type {
  Message,
  Profile,
  Session,
  Settings,
  WorkspaceView,
} from "@/lib/app-types"

type FailedTurn = {
  sessionId: string
  turnId: string
  content: string
}

function visibleError(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "Trellis could not reach the local service."
}

function moveSessionToTop(sessions: Session[], next: Session) {
  return [next, ...sessions.filter((session) => session.id !== next.id)]
}

export function AppShell() {
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [profile, setProfile] = useState<Profile | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failedTurn, setFailedTurn] = useState<FailedTurn | null>(null)

  useEffect(() => {
    let cancelled = false

    const restore = async () => {
      try {
        const [restoredProfile, restoredSettings, restoredSessions] =
          await Promise.all([
            api.getProfile(),
            api.getSettings(),
            api.listSessions(),
          ])
        if (cancelled) return

        setProfile(restoredProfile)
        setSettings(restoredSettings)
        setSessions(restoredSessions)
        if (restoredSessions[0]) {
          const detail = await api.getSession(restoredSessions[0].id)
          if (cancelled) return
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setActiveView("session")
        }
      } catch (restoreError) {
        if (!cancelled) setError(visibleError(restoreError))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void restore()
    return () => {
      cancelled = true
    }
  }, [])

  const startNewSession = useCallback(() => {
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setFailedTurn(null)
    setError(null)
    setActiveView("New session")
    setSidebarOpen(false)
    setSidebarCollapsed(false)
  }, [])

  const handleNavigate = (view: WorkspaceView) => {
    if (view === "New session") {
      startNewSession()
      return
    }
    setActiveView(view)
    setSidebarOpen(false)
  }

  const selectSession = async (sessionId: string) => {
    if (pending) return
    setSidebarOpen(false)
    setActiveView("session")
    setError(null)
    setFailedTurn(null)
    try {
      const detail = await api.getSession(sessionId)
      setActiveSession(detail.session)
      setMessages(detail.messages)
    } catch (sessionError) {
      setError(visibleError(sessionError))
    }
  }

  const toggleSidebar = () => {
    if (window.innerWidth <= 760) {
      setSidebarOpen(false)
      return
    }
    setSidebarCollapsed((collapsed) => !collapsed)
  }

  const completeTurn = async (turn: FailedTurn, optimistic: boolean) => {
    setPending(true)
    setError(null)
    if (optimistic) {
      setMessages((current) => [
        ...current,
        {
          id: `pending-${turn.turnId}`,
          turn_id: turn.turnId,
          role: "user",
          content: turn.content,
          provider: null,
          model: null,
          created_at: new Date().toISOString(),
        },
      ])
    }

    try {
      const result = await api.completeTurn(
        turn.sessionId,
        turn.turnId,
        turn.content
      )
      setActiveSession(result.session)
      setMessages((current) => [
        ...current.filter((message) => message.turn_id !== turn.turnId),
        result.user_message,
        result.assistant_message,
      ])
      setSessions((current) => moveSessionToTop(current, result.session))
      setFailedTurn(null)
      setComposerValue("")
    } catch (turnError) {
      setError(visibleError(turnError))
      setFailedTurn(turn)
      try {
        const detail = await api.getSession(turn.sessionId)
        setActiveSession(detail.session)
        setMessages(detail.messages)
        setSessions((current) => moveSessionToTop(current, detail.session))
      } catch {
        // Keep the optimistic user message if the local transcript cannot be refreshed.
      }
      if (
        turnError instanceof ApiError &&
        turnError.code === "provider_not_configured"
      ) {
        setComposerValue(turn.content)
      }
    } finally {
      setPending(false)
    }
  }

  const submitComposer = async () => {
    const content = composerValue.trim()
    if (!content || pending) return
    const provider = settings?.providers.find(
      (item) => item.id === settings.selected_provider
    )
    if (!provider?.configured) {
      setError(
        provider
          ? `Add an API key for ${provider.name} in Settings.`
          : "Open Settings before starting a session."
      )
      return
    }

    setComposerValue("")
    try {
      let session = activeSession
      if (!session) {
        const createdSession = await api.createSession()
        session = createdSession
        setActiveSession(createdSession)
        setSessions((current) => moveSessionToTop(current, createdSession))
        setActiveView("session")
      }
      await completeTurn(
        {
          sessionId: session.id,
          turnId: crypto.randomUUID(),
          content,
        },
        true
      )
    } catch (submitError) {
      setComposerValue(content)
      setError(visibleError(submitError))
    }
  }

  const selectedProvider = settings?.providers.find(
    (provider) => provider.id === settings.selected_provider
  )
  const modelLabel = selectedProvider?.model ?? "Local chat"

  return (
    <main className="app-shell">
      <button
        className={`mobile-menu ${sidebarCollapsed ? "sidebar-restorer" : ""}`}
        aria-label="Open navigation"
        onClick={() => {
          if (window.innerWidth <= 760) setSidebarOpen(true)
          else setSidebarCollapsed(false)
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
      <div
        className={`sidebar-container ${sidebarOpen ? "open" : ""} ${sidebarCollapsed ? "collapsed" : ""}`}
      >
        <Sidebar
          activeView={activeView}
          sessions={sessions}
          activeSessionId={activeSession?.id ?? null}
          onNavigate={handleNavigate}
          onSelectSession={(sessionId) => void selectSession(sessionId)}
          sidebarCollapsed={sidebarCollapsed}
          onToggleSidebar={toggleSidebar}
        />
      </div>

      <section className="workspace">
        <WorkspaceTopbar activeView={activeView} onNavigate={handleNavigate} />

        <div
          className={`workspace-content ${activeView === "Settings" ? "settings-content" : ""} ${activeView === "session" ? "thread-content" : ""}`}
        >
          {loading ? (
            <div className="workspace-loading" role="status">
              Opening your local workspace…
            </div>
          ) : activeView === "Settings" && profile && settings ? (
            <SettingsPage
              profile={profile}
              settings={settings}
              onProfileChange={setProfile}
              onSettingsChange={setSettings}
            />
          ) : activeView === "session" && activeSession ? (
            <ChatThread
              session={activeSession}
              messages={messages}
              pending={pending}
              error={error}
              canRetry={failedTurn !== null && !pending}
              onRetry={() => {
                if (failedTurn) void completeTurn(failedTurn, false)
              }}
            />
          ) : (
            <div className="welcome-state">
              <WelcomePanel activeView={activeView} activeSessionTitle={null} />
              {error ? (
                <div className="chat-error welcome-error" role="alert">
                  {error}
                </div>
              ) : null}
            </div>
          )}
        </div>

        {!loading && activeView !== "Settings" ? (
          <Composer
            value={composerValue}
            placeholder={
              activeView === "New session"
                ? "What are we building?"
                : "Adjust or continue"
            }
            onChange={setComposerValue}
            onSubmit={() => void submitComposer()}
            disabled={pending}
            modelLabel={modelLabel}
          />
        ) : null}
      </section>
    </main>
  )
}
