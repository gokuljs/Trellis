import { Menu } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

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

function retryFromTranscript(
  sessionId: string,
  messages: Message[]
): FailedTurn | null {
  const lastMessage = messages.at(-1)
  if (!lastMessage || lastMessage.role !== "user") return null
  return {
    sessionId,
    turnId: lastMessage.turn_id,
    content: lastMessage.content,
  }
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
  const [sessionLoading, setSessionLoading] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failedTurn, setFailedTurn] = useState<FailedTurn | null>(null)
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionLoadSequenceRef = useRef(0)
  const submissionLockRef = useRef(false)

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
          const restoredRetry = retryFromTranscript(
            detail.session.id,
            detail.messages
          )
          activeSessionIdRef.current = detail.session.id
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setFailedTurn(restoredRetry)
          if (restoredRetry) {
            setError("The previous assistant response did not complete.")
          }
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
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = null
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setSessionLoading(false)
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
    const loadSequence = sessionLoadSequenceRef.current + 1
    sessionLoadSequenceRef.current = loadSequence
    activeSessionIdRef.current = sessionId
    setSidebarOpen(false)
    setActiveView("session")
    setActiveSession(
      sessions.find((session) => session.id === sessionId) ?? null
    )
    setMessages([])
    setSessionLoading(true)
    setError(null)
    setFailedTurn(null)
    try {
      const detail = await api.getSession(sessionId)
      if (
        sessionLoadSequenceRef.current !== loadSequence ||
        activeSessionIdRef.current !== sessionId
      ) {
        return
      }
      const recoveredRetry = retryFromTranscript(sessionId, detail.messages)
      setActiveSession(detail.session)
      setMessages(detail.messages)
      setFailedTurn(recoveredRetry)
      if (recoveredRetry) {
        setError("The previous assistant response did not complete.")
      }
    } catch (sessionError) {
      if (sessionLoadSequenceRef.current === loadSequence) {
        setError(visibleError(sessionError))
      }
    } finally {
      if (sessionLoadSequenceRef.current === loadSequence) {
        setSessionLoading(false)
      }
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
      setSessions((current) => moveSessionToTop(current, result.session))
      if (activeSessionIdRef.current === turn.sessionId) {
        setActiveSession(result.session)
        setMessages((current) => [
          ...current.filter((message) => message.turn_id !== turn.turnId),
          result.user_message,
          result.assistant_message,
        ])
        setFailedTurn(null)
        setComposerValue("")
      }
    } catch (turnError) {
      if (activeSessionIdRef.current === turn.sessionId) {
        setError(visibleError(turnError))
        setFailedTurn(turn)
      }
      try {
        const detail = await api.getSession(turn.sessionId)
        setSessions((current) => moveSessionToTop(current, detail.session))
        if (activeSessionIdRef.current === turn.sessionId) {
          setActiveSession(detail.session)
          setMessages(detail.messages)
        }
      } catch {
        // Keep the optimistic user message if the local transcript cannot be refreshed.
      }
      if (
        turnError instanceof ApiError &&
        turnError.code === "provider_not_configured"
      ) {
        if (activeSessionIdRef.current === turn.sessionId) {
          setComposerValue(turn.content)
        }
      }
    }
  }

  const submitComposer = async () => {
    const content = composerValue.trim()
    if (!content || pending || sessionLoading || submissionLockRef.current)
      return
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

    submissionLockRef.current = true
    setPending(true)
    setComposerValue("")
    try {
      let session = activeSession
      if (!session) {
        const createdSession = await api.createSession()
        session = createdSession
        sessionLoadSequenceRef.current += 1
        activeSessionIdRef.current = createdSession.id
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
    } finally {
      submissionLockRef.current = false
      setPending(false)
    }
  }

  const retryFailedTurn = async () => {
    if (!failedTurn || pending || submissionLockRef.current) return
    submissionLockRef.current = true
    setPending(true)
    try {
      await completeTurn(failedTurn, false)
    } finally {
      submissionLockRef.current = false
      setPending(false)
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
              onRetry={() => void retryFailedTurn()}
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
            disabled={pending || sessionLoading}
            modelLabel={modelLabel}
          />
        ) : null}
      </section>
    </main>
  )
}
