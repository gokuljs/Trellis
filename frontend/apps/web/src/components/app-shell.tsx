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
import { TestPresets } from "@/components/test-presets"
import { ApiError, api } from "@/lib/api"
import {
  RuntimeError,
  cancelRun,
  resumeRun,
  respondToToolApproval,
  streamRun,
  type RuntimeRunEvent,
  type RuntimeRunInfo,
  type ToolApprovalDecision,
  type TurnRunActivity,
} from "@/lib/runtime-client"
import type {
  LatestRun,
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
  modelId?: string
  budgetPreset?: "conservative" | "longer"
}

function visibleError(error: unknown) {
  return error instanceof ApiError || error instanceof RuntimeError
    ? error.message
    : "Trellis could not reach the local service."
}

function appendSession(sessions: Session[], next: Session) {
  const index = sessions.findIndex((session) => session.id === next.id)
  if (index === -1) return [...sessions, next]
  return sessions.map((session) => (session.id === next.id ? next : session))
}

function replaceSessionInPlace(sessions: Session[], next: Session) {
  const index = sessions.findIndex((session) => session.id === next.id)
  if (index === -1) return [...sessions, next]
  return sessions.map((session) => (session.id === next.id ? next : session))
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

const activeRunStatuses = new Set([
  "queued",
  "running",
  "waiting_for_approval",
  "cancelling",
])
const maxRestoredReconnectAttempts = 4

export function AppShell() {
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [runBudgetChoice, setRunBudgetChoice] = useState<
    "conservative" | "longer" | null
  >(null)
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
  const [runActivities, setRunActivities] = useState<
    Record<string, TurnRunActivity>
  >({})
  const [activeRunTurnId, setActiveRunTurnId] = useState<string | null>(null)
  const [approvalPendingToolId, setApprovalPendingToolId] = useState<
    string | null
  >(null)
  const [approvalError, setApprovalError] = useState<string | null>(null)
  const [reconnectAvailable, setReconnectAvailable] = useState(false)
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
  const runGenerationRef = useRef(0)
  const cancelRequestRef = useRef(false)
  const approvalRequestRef = useRef<string | null>(null)

  const recordRunInfo = useCallback(
    (turnId: string, runInfo: RuntimeRunInfo) =>
      setRunActivities((current) => ({
        ...current,
        [turnId]: {
          events: current[turnId]?.events ?? [],
          runInfo,
        },
      })),
    []
  )

  const recordRunEvent = useCallback(
    (turnId: string, event: RuntimeRunEvent) => {
      if (
        event.eventType !== "assistant.delta" &&
        event.eventType !== "assistant.completed"
      ) {
        setRunActivities((current) => {
          const activity = current[turnId] ?? { events: [], runInfo: null }
          if (activity.events.some((item) => item.sequence === event.sequence))
            return current
          return {
            ...current,
            [turnId]: {
              ...activity,
              events: [...activity.events, event],
            },
          }
        })
      }
      if (event.eventType === "tool.approval_decided") {
        const decidedToolCallId = event.data.tool_call_id
        if (typeof decidedToolCallId === "string") {
          if (approvalRequestRef.current === decidedToolCallId)
            approvalRequestRef.current = null
          setApprovalPendingToolId((current) =>
            current === decidedToolCallId ? null : current
          )
        }
        setApprovalError(null)
      }
      if (
        [
          "run.cancellation_requested",
          "run.completed",
          "run.failed",
          "run.cancelled",
          "run.interrupted",
        ].includes(event.eventType)
      ) {
        setApprovalPendingToolId(null)
        approvalRequestRef.current = null
      }
      if (
        event.eventType === "assistant.delta" &&
        typeof event.data.text === "string"
      ) {
        setStreamingText((current) => (current ?? "") + event.data.text)
      }
    },
    []
  )

  const restoreSessionRun = useCallback(
    async (
      sessionId: string,
      transcript: Message[],
      isCancelled: () => boolean
    ) => {
      const generation = ++runGenerationRef.current
      const isStale = () =>
        isCancelled() || runGenerationRef.current !== generation
      setReconnectAvailable(false)
      setActiveRunTurnId(null)
      setApprovalPendingToolId(null)
      setApprovalError(null)
      approvalRequestRef.current = null
      const waitForDiscovery = (message: string) => {
        setPending(true)
        setStreamingText(null)
        setActiveRunId(null)
        setActiveRunTurnId(null)
        setApprovalPendingToolId(null)
        setApprovalError(null)
        approvalRequestRef.current = null
        activeRunRef.current = null
        setFailedTurn(null)
        setError(message)
        setReconnectAvailable(true)
      }
      let restoredTranscript = transcript
      let turn = retryFromTranscript(sessionId, restoredTranscript)
      let latest: LatestRun | null
      try {
        latest = await api.getLatestRun(sessionId)
      } catch {
        if (isStale()) return
        waitForDiscovery(
          "Trellis could not check the previous run. Reconnect when the local service returns."
        )
        return
      }
      if (isStale()) return
      if (
        latest &&
        !restoredTranscript.some(
          (message) => message.turn_id === latest.turn_id
        )
      ) {
        try {
          const detail = await api.getSession(sessionId)
          if (isStale()) return
          restoredTranscript = detail.messages
          turn = retryFromTranscript(sessionId, restoredTranscript)
          setSessions((current) =>
            replaceSessionInPlace(current, detail.session)
          )
          setActiveSession(detail.session)
          setMessages(detail.messages)
        } catch {
          if (isStale()) return
          waitForDiscovery(
            "Trellis could not load the active run's messages. Reconnect when the local service returns."
          )
          return
        }
        if (
          !restoredTranscript.some(
            (message) => message.turn_id === latest.turn_id
          )
        ) {
          waitForDiscovery(
            "Trellis could not load the active run's messages. Reconnect to try again."
          )
          return
        }
      }
      if (!latest || (turn && latest.turn_id !== turn.turnId)) {
        setPending(false)
        setStreamingText(null)
        setActiveRunId(null)
        activeRunRef.current = null
        setReconnectAvailable(false)
        if (turn) {
          setFailedTurn(turn)
          setError("The previous assistant response did not complete.")
        } else {
          setFailedTurn(null)
          setError(null)
        }
        return
      }

      if (!activeRunStatuses.has(latest.status) && !turn) {
        setPending(false)
        setStreamingText(null)
        setActiveRunId(null)
        setActiveRunTurnId(null)
        activeRunRef.current = null
        setReconnectAvailable(false)
        setCancellingRun(false)
        setError(null)
        return
      }

      setFailedTurn(null)
      setError(null)
      setPending(!!turn || activeRunStatuses.has(latest.status))
      setStreamingText(null)
      setRunActivities((current) => ({
        ...current,
        [latest.turn_id]: { events: [], runInfo: null },
      }))
      setActiveRunTurnId(latest.turn_id)
      setApprovalPendingToolId(null)
      setApprovalError(null)
      approvalRequestRef.current = null
      activeRunRef.current = { runId: latest.run_id, lastSequence: 0 }
      setActiveRunId(latest.run_id)

      void (async () => {
        let keepActive = false
        try {
          let reconnectAttempts = 0
          while (!isStale()) {
            try {
              await resumeRun(
                latest.run_id,
                {
                  reconnectAttempts: 0,
                  onRunId: (runId) => {
                    if (!isStale()) setActiveRunId(runId)
                  },
                  onRunInfo: (runInfo) => {
                    if (!isStale()) recordRunInfo(latest.turn_id, runInfo)
                  },
                  onEvent: (event) => {
                    if (isStale()) return
                    if (activeRunRef.current?.runId !== event.runId) return
                    activeRunRef.current.lastSequence = event.sequence
                    recordRunEvent(latest.turn_id, event)
                  },
                },
                activeRunRef.current?.lastSequence ?? 0
              )
              break
            } catch (streamError) {
              if (isStale()) return
              if (
                !(streamError instanceof RuntimeError) ||
                !["connection_lost", "runtime_timeout"].includes(
                  streamError.code
                )
              ) {
                throw streamError
              }
              setError("Reconnecting to Trellis…")
              let currentStatus: string | null = null
              try {
                const currentRun = await api.getLatestRun(sessionId)
                if (isStale()) return
                if (!currentRun || currentRun.run_id !== latest.run_id) {
                  throw streamError
                }
                currentStatus = currentRun.status
              } catch (lookupError) {
                if (lookupError === streamError) throw streamError
                // The local service may still be restarting; retry the stream.
              }
              reconnectAttempts += 1
              if (reconnectAttempts >= maxRestoredReconnectAttempts) {
                keepActive =
                  currentStatus === null || activeRunStatuses.has(currentStatus)
                if (keepActive) {
                  setError(
                    "Connection to Trellis is unavailable. Reconnect when it returns."
                  )
                  setReconnectAvailable(true)
                  return
                }
                throw streamError
              }
              await new Promise((resolve) =>
                setTimeout(resolve, Math.min(150 * reconnectAttempts, 750))
              )
            }
          }
          if (isStale()) return
          const detail = await api.getSession(sessionId)
          if (isStale()) return
          setSessions((current) =>
            replaceSessionInPlace(current, detail.session)
          )
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setFailedTurn(null)
          setError(null)
          setReconnectAvailable(false)
        } catch (restoreError) {
          if (isStale()) return
          setError(visibleError(restoreError))
          try {
            const currentRun = await api.getLatestRun(sessionId)
            if (isStale()) return
            keepActive =
              currentRun?.run_id === latest.run_id &&
              activeRunStatuses.has(currentRun.status)
            setReconnectAvailable(keepActive)
            if (!keepActive && turn) setFailedTurn(turn)
            const detail = await api.getSession(sessionId)
            if (isStale()) return
            setSessions((current) =>
              replaceSessionInPlace(current, detail.session)
            )
            setActiveSession(detail.session)
            setMessages(detail.messages)
            if (
              !keepActive &&
              !retryFromTranscript(sessionId, detail.messages)
            ) {
              setFailedTurn(null)
              setError(null)
            }
          } catch {
            keepActive = true
            setReconnectAvailable(true)
            // The run may still be active while the local service is unavailable.
          }
        } finally {
          if (!isStale() && !keepActive) {
            setPending(false)
            setStreamingText(null)
            setActiveRunId(null)
            setCancellingRun(false)
            cancelRequestRef.current = false
            activeRunRef.current = null
            setReconnectAvailable(false)
          }
        }
      })()
    },
    [recordRunEvent, recordRunInfo]
  )

  const restoreSessions = useCallback(
    async (isCancelled: () => boolean = () => false) => {
      const restoredSessions = await api.listSessions()
      if (isCancelled()) return

      setSessions(restoredSessions)
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
    runGenerationRef.current += 1
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = null
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setPending(false)
    setStreamingText(null)
    setActiveRunId(null)
    setRunActivities({})
    setActiveRunTurnId(null)
    setApprovalPendingToolId(null)
    setApprovalError(null)
    approvalRequestRef.current = null
    setReconnectAvailable(false)
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
    runGenerationRef.current += 1
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
    setRunActivities({})
    setActiveRunTurnId(null)
    setApprovalPendingToolId(null)
    setApprovalError(null)
    approvalRequestRef.current = null
    setReconnectAvailable(false)
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
      setActiveSession(detail.session)
      setMessages(detail.messages)
      await restoreSessionRun(
        sessionId,
        detail.messages,
        () =>
          sessionLoadSequenceRef.current !== loadSequence ||
          activeSessionIdRef.current !== sessionId
      )
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
    runGenerationRef.current += 1
    setError(null)
    setStreamingText(null)
    setActiveRunId(null)
    setRunActivities((current) => ({
      ...current,
      [turn.turnId]: { events: [], runInfo: null },
    }))
    setActiveRunTurnId(turn.turnId)
    setApprovalPendingToolId(null)
    setApprovalError(null)
    approvalRequestRef.current = null
    setReconnectAvailable(false)
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
          ...(turn.modelId ? { modelId: turn.modelId } : {}),
          ...(turn.budgetPreset ? { budgetPreset: turn.budgetPreset } : {}),
        },
        {
          onRunId: (runId) => {
            activeRunRef.current = { runId, lastSequence: 0 }
            setActiveRunId(runId)
          },
          onRunInfo: (runInfo) => recordRunInfo(turn.turnId, runInfo),
          onEvent: (event) => {
            if (activeRunRef.current?.runId !== event.runId) return
            activeRunRef.current.lastSequence = event.sequence
            recordRunEvent(turn.turnId, event)
          },
        }
      )
      const detail = await api.getSession(turn.sessionId)
      setSessions((current) => replaceSessionInPlace(current, detail.session))
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
        setSessions((current) => replaceSessionInPlace(current, detail.session))
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

  const answerToolApproval = async (
    toolCallId: string,
    decision: ToolApprovalDecision
  ) => {
    const activeRun = activeRunRef.current
    if (
      !activeRun ||
      approvalRequestRef.current !== null ||
      approvalPendingToolId
    )
      return
    approvalRequestRef.current = toolCallId
    setApprovalPendingToolId(toolCallId)
    setApprovalError(null)
    try {
      await respondToToolApproval(
        activeRun.runId,
        toolCallId,
        decision,
        activeRun.lastSequence
      )
    } catch (responseError) {
      if (approvalRequestRef.current === toolCallId) {
        setApprovalError(visibleError(responseError))
        setApprovalPendingToolId((current) =>
          current === toolCallId ? null : current
        )
      }
    } finally {
      if (approvalRequestRef.current === toolCallId)
        approvalRequestRef.current = null
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
    const model = settings?.models?.find((item) => item.id === selectedModelId)
    const provider = settings?.providers.find(
      (item) => item.id === model?.provider_id
    )
    const configured = provider?.configured ?? model?.configured ?? false
    if (!model || (model.requires_api_key && !configured)) {
      setError(
        model
          ? "The selected model needs to be configured in Settings before starting a session."
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
        setSessions((current) => appendSession(current, createdSession))
        setActiveView("session")
      }
      await completeTurn(
        {
          sessionId: session.id,
          turnId: crypto.randomUUID(),
          content,
          modelId: model.id,
          budgetPreset: selectedBudgetPreset,
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

  const reconnectRestoredRun = () => {
    const sessionId = activeSessionIdRef.current
    if (!sessionId || !reconnectAvailable) return
    const recovery = restoreSessionRun(
      sessionId,
      messages,
      () => activeSessionIdRef.current !== sessionId
    )
    const generation = runGenerationRef.current
    void recovery.catch((restoreError) => {
      if (
        activeSessionIdRef.current !== sessionId ||
        runGenerationRef.current !== generation
      )
        return
      setError(visibleError(restoreError))
      setReconnectAvailable(true)
    })
  }

  const configuredModels = (settings?.models ?? []).filter(
    (model) => model.configured || !model.requires_api_key
  )
  const selectedModelId =
    settings?.selected_model_id ?? configuredModels[0]?.id ?? ""
  const selectedBudgetPreset =
    runBudgetChoice ?? settings?.default_budget_preset ?? "conservative"

  const persistWorkspace = async (path: string) => {
    if (activeSession) {
      const updated = await api.setSessionWorkspace(activeSession.id, path)
      setActiveSession(updated)
      setSessions((current) => replaceSessionInPlace(current, updated))
      return
    }
    const created = await api.createSession(path)
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = created.id
    setActiveSession(created)
    setSessions((current) => appendSession(current, created))
    setActiveView("session")
  }

  const saveWorkspace = async (path: string) => {
    workspaceSaveLockRef.current = true
    setWorkspaceSaving(true)
    try {
      await persistWorkspace(path)
    } finally {
      workspaceSaveLockRef.current = false
      setWorkspaceSaving(false)
    }
  }

  const pickWorkspace = async () => {
    workspaceSaveLockRef.current = true
    setWorkspaceSaving(true)
    try {
      const { path } = await api.pickWorkspace()
      if (path === null) return false
      await persistWorkspace(path)
      return true
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
      setSessions((current) => replaceSessionInPlace(current, updated))
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
              canReconnect={reconnectAvailable}
              canCancel={pending && activeRunId !== null}
              cancellationPending={cancellingRun}
              runActivities={runActivities}
              activeRunTurnId={activeRunTurnId}
              approvalPendingToolId={approvalPendingToolId}
              approvalError={approvalError}
              onApprovalDecision={(toolCallId, decision) =>
                void answerToolApproval(toolCallId, decision)
              }
              onRetry={() => void retryFailedTurn()}
              onReconnect={reconnectRestoredRun}
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
              workspacePath={activeSession?.workspace_path ?? null}
              workspaceSessionKey={activeSession?.id ?? "new-session"}
              onChange={setComposerValue}
              onSubmit={() => void submitComposer()}
              onPickWorkspace={pickWorkspace}
              onSaveWorkspace={saveWorkspace}
              onRemoveWorkspace={removeWorkspace}
              disabled={pending || sessionLoading || workspaceSaving}
              budgetPreset={selectedBudgetPreset}
              onBudgetChange={setRunBudgetChoice}
            />
          </>
        ) : null}
      </section>
    </main>
  )
}
