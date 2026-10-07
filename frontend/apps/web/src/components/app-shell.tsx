import { Menu } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { ChatThread } from "@/components/chat-thread"
import { Composer } from "@/components/composer"
import {
  OnboardingFlow,
  type OnboardingValues,
} from "@/components/onboarding-flow"
import { SettingsPage } from "@/components/settings-page"
import { Sidebar } from "@/components/sidebar"
import { WelcomePanel } from "@/components/welcome-panel"
import { WorkspaceTopbar } from "@/components/workspace-topbar"
import { WorkspaceAttachment } from "@/components/workspace-attachment"
import { TestPresets } from "@/components/test-presets"
import { ApiError, api } from "@/lib/api"
import { RuntimeError, cancelRun, streamRun } from "@/lib/runtime-client"
import type {
  Message,
  OnboardingStep,
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
  return error instanceof ApiError || error instanceof RuntimeError
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
  const [onboardingRequired, setOnboardingRequired] = useState(false)
  const [onboardingStep, setOnboardingStep] = useState<OnboardingStep>("intro")
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(true)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [workspaceSaving, setWorkspaceSaving] = useState(false)
  const [pending, setPending] = useState(false)
  const [streamingText, setStreamingText] = useState<string | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [cancellingRun, setCancellingRun] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failedTurn, setFailedTurn] = useState<FailedTurn | null>(null)
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionLoadSequenceRef = useRef(0)
  const submissionLockRef = useRef(false)
  const workspaceSaveLockRef = useRef(false)
  const activeRunRef = useRef<{ runId: string; lastSequence: number } | null>(
    null
  )
  const cancelRequestRef = useRef(false)

  const restoreSessions = useCallback(
    async (isCancelled: () => boolean = () => false) => {
      const restoredSessions = await api.listSessions()
      if (isCancelled()) return

      setSessions(restoredSessions)
      if (!restoredSessions[0]) return

      const detail = await api.getSession(restoredSessions[0].id)
      if (isCancelled()) return
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
    },
    []
  )

  useEffect(() => {
    let cancelled = false

    const restore = async () => {
      try {
        const [restoredProfile, restoredSettings, restoredOnboarding] =
          await Promise.all([
            api.getProfile(),
            api.getSettings(),
            api.getOnboarding(),
          ])
        if (cancelled) return

        setProfile(restoredProfile)
        setSettings(restoredSettings)
        if (!restoredOnboarding.completed) {
          setOnboardingRequired(true)
          setOnboardingStep(
            restoredOnboarding.current_step === "complete"
              ? "intro"
              : restoredOnboarding.current_step
          )
          return
        }
        await restoreSessions(() => cancelled)
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
  }, [restoreSessions])

  const completeOnboarding = async (values: OnboardingValues) => {
    if (!profile || !settings) {
      throw new Error("Trellis is still loading your local settings.")
    }

    const progress = await api.saveOnboardingModel(
      values.modelId,
      values.apiKey
    )
    if (!progress.completed)
      throw new Error("Trellis could not complete setup.")
    setSettings(await api.getSettings())
    await restoreSessions()
    setOnboardingRequired(false)
  }

  const advanceOnboardingIntro = async () => {
    const progress = await api.completeOnboardingIntro()
    setOnboardingStep(
      progress.current_step === "complete" ? "intro" : progress.current_step
    )
  }

  const saveOnboardingProfile = async (displayName: string, email: string) => {
    const progress = await api.saveOnboardingProfile({
      display_name: displayName,
      email,
    })
    setProfile((current) =>
      current ? { ...current, display_name: displayName, email } : current
    )
    setOnboardingStep(
      progress.current_step === "complete" ? "intro" : progress.current_step
    )
  }

  const startNewSession = useCallback(() => {
    if (workspaceSaveLockRef.current) return
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = null
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setStreamingText(null)
    setActiveRunId(null)
    setCancellingRun(false)
    cancelRequestRef.current = false
    activeRunRef.current = null
    setSessionLoading(false)
    setFailedTurn(null)
    setError(null)
    setActiveView("New session")
    setSidebarOpen(false)
    setSidebarCollapsed(false)
  }, [])

  const handleNavigate = (view: WorkspaceView) => {
    if (workspaceSaveLockRef.current) return
    if (view === "New session") {
      startNewSession()
      return
    }
    setActiveView(view)
    setSidebarOpen(false)
  }

  const selectSession = async (sessionId: string) => {
    if (pending || workspaceSaveLockRef.current) return
    const loadSequence = sessionLoadSequenceRef.current + 1
    sessionLoadSequenceRef.current = loadSequence
    activeSessionIdRef.current = sessionId
    setSidebarOpen(false)
    setActiveView("session")
    setActiveSession(
      sessions.find((session) => session.id === sessionId) ?? null
    )
    setMessages([])
    setStreamingText(null)
    setActiveRunId(null)
    setCancellingRun(false)
    cancelRequestRef.current = false
    activeRunRef.current = null
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
    setStreamingText(null)
    setActiveRunId(null)
    setCancellingRun(false)
    cancelRequestRef.current = false
    activeRunRef.current = null
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
      await streamRun(
        {
          sessionId: turn.sessionId,
          turnId: turn.turnId,
          clientRequestId: crypto.randomUUID(),
          content: turn.content,
        },
        {
          onRunId: (runId) => {
            activeRunRef.current = { runId, lastSequence: 0 }
            setActiveRunId(runId)
          },
          onEvent: (event) => {
            if (activeRunRef.current?.runId === event.runId) {
              activeRunRef.current.lastSequence = event.sequence
            }
            if (
              event.eventType === "assistant.delta" &&
              typeof event.data.text === "string"
            ) {
              setStreamingText((current) => (current ?? "") + event.data.text)
            }
          },
        }
      )
      const detail = await api.getSession(turn.sessionId)
      setSessions((current) => moveSessionToTop(current, detail.session))
      if (activeSessionIdRef.current === turn.sessionId) {
        setActiveSession(detail.session)
        setMessages(detail.messages)
        setFailedTurn(null)
        setComposerValue("")
      }
      setStreamingText(null)
      setActiveRunId(null)
      setCancellingRun(false)
      cancelRequestRef.current = false
      activeRunRef.current = null
    } catch (turnError) {
      setStreamingText(null)
      setActiveRunId(null)
      setCancellingRun(false)
      cancelRequestRef.current = false
      activeRunRef.current = null
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
        (turnError instanceof ApiError || turnError instanceof RuntimeError) &&
        turnError.code === "provider_not_configured"
      ) {
        if (activeSessionIdRef.current === turn.sessionId) {
          setComposerValue(turn.content)
        }
      }
    }
  }

  const cancelActiveRun = async () => {
    const activeRun = activeRunRef.current
    if (!activeRun || cancelRequestRef.current) return
    cancelRequestRef.current = true
    setCancellingRun(true)
    try {
      await cancelRun(activeRun.runId, activeRun.lastSequence)
    } catch (cancelError) {
      cancelRequestRef.current = false
      setCancellingRun(false)
      if (activeSessionIdRef.current) setError(visibleError(cancelError))
    }
  }

  const submitComposer = async () => {
    const content = composerValue.trim()
    if (
      !content ||
      pending ||
      sessionLoading ||
      submissionLockRef.current ||
      workspaceSaveLockRef.current
    )
      return
    const model = settings?.models?.find(
      (item) => item.id === settings.selected_model_id
    )
    const provider = settings?.providers.find(
      (item) => item.id === model?.provider_id
    )
    const configured = provider?.configured ?? model?.configured ?? false
    if (!model || (model.requires_api_key && !configured)) {
      setError(
        model
          ? `Add an API key for ${model.provider_name} in Settings.`
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

  const selectedModel = settings?.models?.find(
    (model) => model.id === settings.selected_model_id
  )
  const modelLabel = selectedModel?.name ?? "Local chat"

  const saveWorkspace = async (path: string) => {
    workspaceSaveLockRef.current = true
    setWorkspaceSaving(true)
    try {
      if (activeSession) {
        const updated = await api.setSessionWorkspace(activeSession.id, path)
        setActiveSession(updated)
        setSessions((current) => moveSessionToTop(current, updated))
        return
      }
      const created = await api.createSession(path)
      sessionLoadSequenceRef.current += 1
      activeSessionIdRef.current = created.id
      setActiveSession(created)
      setSessions((current) => moveSessionToTop(current, created))
      setActiveView("session")
    } finally {
      workspaceSaveLockRef.current = false
      setWorkspaceSaving(false)
    }
  }

  const removeWorkspace = async () => {
    if (!activeSession) return
    workspaceSaveLockRef.current = true
    setWorkspaceSaving(true)
    try {
      const updated = await api.setSessionWorkspace(activeSession.id, null)
      setActiveSession(updated)
      setSessions((current) => moveSessionToTop(current, updated))
    } finally {
      workspaceSaveLockRef.current = false
      setWorkspaceSaving(false)
    }
  }

  if (loading) {
    return (
      <main
        className="onboarding-shell onboarding-loading-shell"
        data-theme="dark"
      >
        <div className="workspace-loading" role="status">
          Opening your local workspace…
        </div>
      </main>
    )
  }

  if (onboardingRequired && profile && settings) {
    return (
      <OnboardingFlow
        profile={profile}
        settings={settings}
        initialStep={onboardingStep}
        onAdvanceIntro={advanceOnboardingIntro}
        onSaveProfile={saveOnboardingProfile}
        onComplete={completeOnboarding}
      />
    )
  }

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
        <WorkspaceTopbar
          activeView={activeView}
          activeSessionTitle={activeSession?.title ?? null}
          onNavigate={handleNavigate}
        />

        <div
          className={`workspace-content ${activeView === "Settings" ? "settings-content" : ""} ${activeView === "session" ? "thread-content" : ""}`}
        >
          {activeView === "Settings" && profile && settings ? (
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
              streamingText={streamingText}
              error={error}
              canRetry={failedTurn !== null && !pending}
              canCancel={pending && activeRunId !== null}
              cancellationPending={cancellingRun}
              onRetry={() => void retryFailedTurn()}
              onCancel={() => void cancelActiveRun()}
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

        {activeView !== "Settings" ? (
          <>
            <WorkspaceAttachment
              key={activeSession?.id ?? "new-session"}
              workspacePath={activeSession?.workspace_path ?? null}
              disabled={pending || sessionLoading || workspaceSaving}
              onSave={saveWorkspace}
              onRemove={removeWorkspace}
            />
            {activeSession?.workspace_path ? (
              <TestPresets
                key={`${activeSession.id}:${activeSession.workspace_path}`}
                sessionId={activeSession.id}
                disabled={pending || sessionLoading || workspaceSaving}
              />
            ) : null}
            <Composer
              value={composerValue}
              placeholder={
                activeView === "New session"
                  ? "What are we building?"
                  : "Adjust or continue"
              }
              onChange={setComposerValue}
              onSubmit={() => void submitComposer()}
              disabled={pending || sessionLoading || workspaceSaving}
              modelLabel={modelLabel}
            />
          </>
        ) : null}
      </section>
    </main>
  )
}
