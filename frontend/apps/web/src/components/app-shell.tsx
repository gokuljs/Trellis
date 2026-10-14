import { Menu } from "lucide-react"
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"

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
import { ApiError, api } from "@/lib/api"
import {
  mergeSavedAssistantOutputs,
  newAssistantOutputState,
  reduceAssistantOutputEvent,
} from "@/lib/assistant-outputs"
import {
  RuntimeError,
  cancelRun,
  mergeRunEvents,
  resumeRun,
  respondToToolApproval,
  streamRun,
  type RuntimeRunEvent,
  type RuntimeRunInfo,
  type RunAttemptActivity,
  type ToolApprovalDecision,
  type TurnRunActivity,
} from "@/lib/runtime-client"
import type {
  LatestRun,
  Message,
  OnboardingState,
  Profile,
  Session,
  RunSummary,
  SavedRunEvent,
  Settings,
  WorkspaceView,
} from "@/lib/app-types"
import {
  ownsRun,
  recoveryTranscript,
  turnFailureAction,
  type TerminalRunOutcome,
} from "@/lib/run-recovery"
import "./workspace-bootstrap.css"

type BootstrapState =
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "onboarding"; progress: OnboardingState }
  | { status: "ready"; progress: OnboardingState }

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
    : "Trellis could not reach the service."
}

function upsertSession(sessions: Session[], next: Session) {
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

function runInfoFromSummary(summary: RunSummary): RuntimeRunInfo {
  return {
    runId: summary.run_id,
    budgetPreset: summary.budget_preset,
    limits: {
      maxModelCalls: summary.max_model_calls,
      maxToolCalls: summary.max_tool_calls,
      maxTotalTokens: summary.max_total_tokens,
      maxCostUsd: summary.max_cost_usd,
      deadlineAt: summary.deadline_at,
    },
  }
}

function activitiesFromSummaries(
  summaries: RunSummary[]
): Record<string, TurnRunActivity> {
  const byTurn = new Map<string, RunSummary[]>()
  for (const summary of summaries) {
    const attempts = byTurn.get(summary.turn_id) ?? []
    attempts.push(summary)
    byTurn.set(summary.turn_id, attempts)
  }
  return Object.fromEntries(
    [...byTurn].map(([turnId, attempts]) => {
      const latest = attempts.at(-1)!
      return [
        turnId,
        {
          events: [],
          runInfo: runInfoFromSummary(latest),
          summary: latest,
          priorAttempts: attempts
            .slice(0, -1)
            .map((summary): RunAttemptActivity => ({
              runId: summary.run_id,
              summary,
              runInfo: runInfoFromSummary(summary),
              events: null,
            })),
        },
      ]
    })
  )
}

async function loadSessionRunSummaries(
  sessionId: string
): Promise<RunSummary[]> {
  const runs: RunSummary[] = []
  let offset = 0
  while (true) {
    const page = await api.listRunSummaries(sessionId, offset)
    runs.push(...page.items)
    if (page.next_offset === null) return runs
    if (page.next_offset <= offset) throw new Error("Invalid run history page")
    offset = page.next_offset
  }
}

function savedRunEvent(event: SavedRunEvent): RuntimeRunEvent {
  return {
    runId: event.run_id,
    sequence: event.sequence,
    eventType: event.event_type,
    eventVersion: event.event_version,
    data: event.data,
    createdAt: event.created_at,
  }
}

async function loadRunEventHistory(
  sessionId: string,
  runId: string
): Promise<RuntimeRunEvent[]> {
  const events: RuntimeRunEvent[] = []
  let afterSequence = 0
  while (true) {
    const page = await api.listRunEvents(sessionId, runId, afterSequence)
    events.push(...page.items.map(savedRunEvent))
    if (page.next_after_sequence === null) return events
    if (page.next_after_sequence <= afterSequence)
      throw new Error("Invalid run event page")
    afterSequence = page.next_after_sequence
  }
}

function startNextAttempt(previous?: TurnRunActivity): TurnRunActivity {
  const priorAttempts = [...(previous?.priorAttempts ?? [])]
  const runId = previous?.runInfo?.runId ?? previous?.summary?.run_id
  if (runId && !priorAttempts.some((attempt) => attempt.runId === runId)) {
    priorAttempts.push({
      runId,
      summary: previous?.summary ?? null,
      runInfo: previous?.runInfo ?? null,
      events: previous?.events.length ? previous.events : null,
    })
  }
  return { events: [], runInfo: null, priorAttempts }
}

export function AppShell({
  accountControl,
}: {
  accountControl?: ReactNode | ((profile: Profile | null) => ReactNode)
} = {}) {
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [runBudgetChoice, setRunBudgetChoice] = useState<
    "conservative" | "longer" | null
  >(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [bootstrap, setBootstrap] = useState<BootstrapState>({
    status: "loading",
  })
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [assistantOutputState, setAssistantOutputState] = useState(
    newAssistantOutputState
  )
  const [sessionLoading, setSessionLoading] = useState(false)
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
  const [activityHistoryError, setActivityHistoryError] = useState<
    string | null
  >(null)
  const [failedTurn, setFailedTurn] = useState<FailedTurn | null>(null)
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionLoadSequenceRef = useRef(0)
  const submissionLockRef = useRef(false)
  const activeRunRef = useRef<{ runId: string; lastSequence: number } | null>(
    null
  )
  const runGenerationRef = useRef(0)
  const cancelRequestRef = useRef(false)
  const approvalRequestRef = useRef<string | null>(null)
  const fetchingRunIdsRef = useRef(new Set<string>())
  const bootstrapControllerRef = useRef<AbortController | null>(null)

  const recordRunInfo = useCallback(
    (turnId: string, runInfo: RuntimeRunInfo) =>
      setRunActivities((current) => ({
        ...current,
        [turnId]: {
          ...current[turnId],
          events: current[turnId]?.events ?? [],
          runInfo,
          latestEvent: current[turnId]?.latestEvent,
        },
      })),
    []
  )

  const recordRunEvent = useCallback(
    (turnId: string, event: RuntimeRunEvent) => {
      setAssistantOutputState((current) =>
        reduceAssistantOutputEvent(current, event, turnId)
      )
      setRunActivities((current) => {
        const activity = current[turnId] ?? { events: [], runInfo: null }
        if (activity.runInfo && activity.runInfo.runId !== event.runId)
          return current
        const events = !activity.events.some(
          (item) =>
            item.runId === event.runId && item.sequence === event.sequence
        )
          ? [...activity.events, event]
          : activity.events
        return {
          ...current,
          [turnId]: { ...activity, events, latestEvent: event },
        }
      })
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
        typeof event.data.output_id !== "string" &&
        typeof event.data.text === "string"
      ) {
        setStreamingText((current) => (current ?? "") + event.data.text)
      }
    },
    []
  )

  const loadTurnEvents = (turnId: string) => {
    const sessionId = activeSessionIdRef.current
    const activity = runActivities[turnId]
    if (!sessionId || !activity) return
    const needed = [
      ...(activity.priorAttempts ?? [])
        .filter((attempt) => attempt.events === null)
        .map((attempt) => attempt.runId),
      ...(activity.summary && activity.events.length === 0
        ? [activity.summary.run_id]
        : []),
    ]
    void (async () => {
      for (const runId of needed) {
        if (fetchingRunIdsRef.current.has(runId)) continue
        fetchingRunIdsRef.current.add(runId)
        setRunActivities((current) => {
          const turn = current[turnId]
          if (!turn) return current
          return {
            ...current,
            [turnId]: {
              ...turn,
              eventsLoading:
                turn.summary?.run_id === runId || turn.eventsLoading,
              eventsError:
                turn.summary?.run_id === runId ? null : turn.eventsError,
              priorAttempts: turn.priorAttempts?.map((attempt) =>
                attempt.runId === runId
                  ? { ...attempt, loading: true, error: null }
                  : attempt
              ),
            },
          }
        })
        try {
          const events = await loadRunEventHistory(sessionId, runId)
          if (activeSessionIdRef.current !== sessionId) return
          setRunActivities((current) => {
            const turn = current[turnId]
            if (!turn) return current
            const merged = mergeRunEvents(runId, events, turn.events)
            const mostRecent = merged.at(-1)
            const latestEvent =
              turn.latestEvent?.runId === runId &&
              turn.latestEvent.sequence > (mostRecent?.sequence ?? 0)
                ? turn.latestEvent
                : mostRecent
            return {
              ...current,
              [turnId]: {
                ...turn,
                ...(turn.summary?.run_id === runId
                  ? {
                      events: merged,
                      latestEvent,
                      eventsLoading: false,
                      eventsError: null,
                    }
                  : {}),
                priorAttempts: turn.priorAttempts?.map((attempt) =>
                  attempt.runId === runId
                    ? { ...attempt, events, loading: false, error: null }
                    : attempt
                ),
              },
            }
          })
        } catch {
          if (activeSessionIdRef.current !== sessionId) return
          setRunActivities((current) => {
            const turn = current[turnId]
            if (!turn) return current
            return {
              ...current,
              [turnId]: {
                ...turn,
                eventsLoading:
                  turn.summary?.run_id === runId ? false : turn.eventsLoading,
                eventsError:
                  turn.summary?.run_id === runId
                    ? "Could not load activity."
                    : turn.eventsError,
                priorAttempts: turn.priorAttempts?.map((attempt) =>
                  attempt.runId === runId
                    ? {
                        ...attempt,
                        loading: false,
                        error: "Could not load this attempt.",
                      }
                    : attempt
                ),
              },
            }
          })
        } finally {
          fetchingRunIdsRef.current.delete(runId)
        }
      }
    })()
  }

  const restoreSessionRun = useCallback(
    async (
      sessionId: string,
      transcript: Message[],
      isCancelled: () => boolean,
      ownerGeneration?: number
    ) => {
      const generation = ownerGeneration ?? ++runGenerationRef.current
      const isStale = () =>
        isCancelled() || runGenerationRef.current !== generation
      setReconnectAvailable(false)
      if (!activeRunRef.current) setActiveRunTurnId(null)
      setApprovalPendingToolId(null)
      setApprovalError(null)
      approvalRequestRef.current = null
      const waitForDiscovery = (message: string) => {
        setPending(true)
        setStreamingText(null)
        if (!activeRunRef.current) {
          setActiveRunId(null)
          setActiveRunTurnId(null)
        }
        setApprovalPendingToolId(null)
        setApprovalError(null)
        approvalRequestRef.current = null
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
          "Trellis could not check the previous run. Reconnect when the service returns."
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
          setSessions((current) => upsertSession(current, detail.session))
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setAssistantOutputState((current) =>
            mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
          )
        } catch {
          if (isStale()) return
          waitForDiscovery(
            "Trellis could not load the active run's messages. Reconnect when the service returns."
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

      if (!activeRunStatuses.has(latest.status)) {
        setPending(false)
        setStreamingText(null)
        setActiveRunId(null)
        setActiveRunTurnId(null)
        activeRunRef.current = null
        setReconnectAvailable(false)
        setCancellingRun(false)
        if (turn && latest.status !== "completed") {
          setFailedTurn(turn)
          setError("The previous assistant response did not complete.")
        } else {
          setFailedTurn(null)
          setError(null)
        }
        return
      }

      setFailedTurn(null)
      setError(null)
      setPending(!!turn || activeRunStatuses.has(latest.status))
      setStreamingText(null)
      setRunActivities((current) => {
        const previous = current[latest.turn_id]
        const previousRunId =
          previous?.runInfo?.runId ?? previous?.summary?.run_id
        const aligned =
          previousRunId && previousRunId !== latest.run_id
            ? startNextAttempt(previous)
            : previous
        return {
          ...current,
          [latest.turn_id]: {
            ...aligned,
            events: [],
            latestEvent: undefined,
            runInfo: {
              ...(aligned?.runInfo ?? {}),
              runId: latest.run_id,
            },
          },
        }
      })
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
                // The service may still be restarting; retry the stream.
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
          setSessions((current) => upsertSession(current, detail.session))
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setAssistantOutputState((current) =>
            mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
          )
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
            setSessions((current) => upsertSession(current, detail.session))
            setActiveSession(detail.session)
            setMessages(detail.messages)
            setAssistantOutputState((current) =>
              mergeSavedAssistantOutputs(
                current,
                detail.assistant_outputs ?? []
              )
            )
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
            // The run may still be active while the service is unavailable.
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

  const restoreWorkspace = useCallback(async () => {
    bootstrapControllerRef.current?.abort()
    const controller = new AbortController()
    bootstrapControllerRef.current = controller
    const options = { signal: controller.signal }
    const timer = setTimeout(
      () =>
        controller.abort(
          new ApiError(
            "request_timeout",
            "Trellis took too long to load your chats. Try again.",
            0
          )
        ),
      15000
    )
    const current = () => bootstrapControllerRef.current === controller
    try {
      const [restoredProfile, restoredSettings, restoredOnboarding] =
        await Promise.all([
          api.getProfile(options),
          api.getSettings(options),
          api.getOnboarding(options),
        ])
      if (!current()) return

      setProfile(restoredProfile)
      setSettings(restoredSettings)
      if (!restoredOnboarding.completed) {
        setBootstrap({ status: "onboarding", progress: restoredOnboarding })
        return
      }
      const restoredSessions = await api.listSessions(options)
      if (!current()) return
      setSessions(restoredSessions)
      setBootstrap({ status: "ready", progress: restoredOnboarding })
    } catch (restoreError) {
      if (current()) {
        setBootstrap({ status: "failed", message: visibleError(restoreError) })
        controller.abort()
      }
    } finally {
      clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    let mounted = true
    void Promise.resolve().then(() => {
      if (mounted) void restoreWorkspace()
    })
    return () => {
      mounted = false
      bootstrapControllerRef.current?.abort()
      bootstrapControllerRef.current = null
    }
  }, [restoreWorkspace])

  const completeOnboarding = async (values: OnboardingValues) => {
    if (!profile || !settings) {
      throw new Error("Trellis is still loading your settings.")
    }

    const controller = bootstrapControllerRef.current
    if (!controller) return
    const progress = await api.saveOnboardingModel(values.modelId, {
      signal: controller.signal,
    })
    if (
      bootstrapControllerRef.current !== controller ||
      controller.signal.aborted
    )
      return
    if (!progress.completed)
      throw new Error("Trellis could not complete setup.")
    setBootstrap({ status: "loading" })
    await restoreWorkspace()
  }

  const advanceOnboardingIntro = async () => {
    const progress = await api.completeOnboardingIntro({
      signal: bootstrapControllerRef.current?.signal,
    })
    setBootstrap({ status: "onboarding", progress })
  }

  const saveOnboardingProfile = async (displayName: string) => {
    const options = { signal: bootstrapControllerRef.current?.signal }
    const progress = await api.saveOnboardingProfile(
      {
        display_name: displayName,
      },
      options
    )
    setProfile(await api.getProfile(options))
    setBootstrap({ status: "onboarding", progress })
  }

  const startNewSession = useCallback(() => {
    runGenerationRef.current += 1
    submissionLockRef.current = false
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = null
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setAssistantOutputState(newAssistantOutputState())
    setPending(false)
    setStreamingText(null)
    setActiveRunId(null)
    setRunActivities({})
    fetchingRunIdsRef.current.clear()
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
    setActivityHistoryError(null)
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
    setAssistantOutputState(newAssistantOutputState())
    setStreamingText(null)
    setActiveRunId(null)
    setRunActivities({})
    fetchingRunIdsRef.current.clear()
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
    setActivityHistoryError(null)
    setFailedTurn(null)
    try {
      const [detail, history] = await Promise.all([
        api.getSession(sessionId),
        loadSessionRunSummaries(sessionId).then(
          (summaries) => ({ summaries, error: null }),
          () => ({
            summaries: [] as RunSummary[],
            error: "Saved activity could not be loaded.",
          })
        ),
      ])
      if (
        sessionLoadSequenceRef.current !== loadSequence ||
        activeSessionIdRef.current !== sessionId
      ) {
        return
      }
      setActiveSession(detail.session)
      setMessages(detail.messages)
      setAssistantOutputState((current) =>
        mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
      )
      setRunActivities(activitiesFromSummaries(history.summaries))
      setActivityHistoryError(history.error)
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
    const owner = {
      sessionId: turn.sessionId,
      generation: ++runGenerationRef.current,
    }
    const isCurrent = () =>
      ownsRun(owner, {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
      })
    setError(null)
    setStreamingText(null)
    setActiveRunId(null)
    setRunActivities((current) => ({
      ...current,
      [turn.turnId]: startNextAttempt(current[turn.turnId]),
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

    const lifecycle: { terminal: TerminalRunOutcome | null } = {
      terminal: null,
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
            if (!isCurrent()) return
            activeRunRef.current = { runId, lastSequence: 0 }
            setActiveRunId(runId)
          },
          onRunInfo: (runInfo) => {
            if (isCurrent()) recordRunInfo(turn.turnId, runInfo)
          },
          onEvent: (event) => {
            if (!isCurrent()) return
            if (activeRunRef.current?.runId !== event.runId) return
            activeRunRef.current.lastSequence = event.sequence
            if (event.eventType === "run.completed")
              lifecycle.terminal = "completed"
            else if (event.eventType === "run.failed")
              lifecycle.terminal = "failed"
            else if (event.eventType === "run.cancelled")
              lifecycle.terminal = "cancelled"
            else if (event.eventType === "run.interrupted")
              lifecycle.terminal = "interrupted"
            recordRunEvent(turn.turnId, event)
          },
        }
      )
      if (!isCurrent()) return true
      lifecycle.terminal = "completed"
      setActiveRunId(null)
      activeRunRef.current = null
      const detail = await api.getSession(turn.sessionId)
      if (!isCurrent()) return true
      setSessions((current) => upsertSession(current, detail.session))
      if (activeSessionIdRef.current === turn.sessionId) {
        setActiveSession(detail.session)
        setMessages(detail.messages)
        setAssistantOutputState((current) =>
          mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
        )
        setFailedTurn(null)
        setComposerValue("")
      }
      setStreamingText(null)
      setActiveRunId(null)
      setCancellingRun(false)
      cancelRequestRef.current = false
      activeRunRef.current = null
    } catch (turnError) {
      if (!isCurrent()) return true
      const failureAction = turnFailureAction(
        turnError instanceof ApiError || turnError instanceof RuntimeError
          ? turnError.code
          : undefined,
        lifecycle.terminal
      )
      if (
        failureAction === "reconnect" &&
        activeSessionIdRef.current === turn.sessionId
      ) {
        setPending(true)
        setFailedTurn(null)
        setError("Connection to Trellis is unavailable. Reconnecting…")
        setReconnectAvailable(false)
        let transcript = recoveryTranscript(messages, turn)
        try {
          const detail = await api.getSession(turn.sessionId)
          if (!isCurrent()) return true
          transcript = detail.messages
          setSessions((current) => upsertSession(current, detail.session))
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setAssistantOutputState((current) =>
            mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
          )
        } catch {
          if (!isCurrent()) return true
          // Discovery still has the accepted turn when its transcript is unavailable.
        }
        await restoreSessionRun(
          turn.sessionId,
          transcript,
          () => !isCurrent(),
          owner.generation
        )
        return true
      }
      setStreamingText(null)
      setActiveRunId(null)
      setCancellingRun(false)
      cancelRequestRef.current = false
      activeRunRef.current = null
      if (activeSessionIdRef.current === turn.sessionId) {
        setError(
          failureAction === "reload"
            ? "Your answer is saved, but Trellis could not reload the messages. Reconnect to try again."
            : visibleError(turnError)
        )
        setFailedTurn(failureAction === "reload" ? null : turn)
        if (failureAction === "reload") {
          setReconnectAvailable(true)
          return false
        }
      }
      try {
        const detail = await api.getSession(turn.sessionId)
        if (!isCurrent()) return true
        setSessions((current) => upsertSession(current, detail.session))
        if (activeSessionIdRef.current === turn.sessionId) {
          setActiveSession(detail.session)
          setMessages(detail.messages)
          setAssistantOutputState((current) =>
            mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
          )
        }
      } catch {
        if (!isCurrent()) return true
        // Keep the optimistic user message if the saved transcript cannot be refreshed.
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
    return false
  }

  const cancelActiveRun = async () => {
    const activeRun = activeRunRef.current
    if (!activeRun || cancelRequestRef.current) return
    const owner = {
      sessionId: activeSessionIdRef.current,
      generation: runGenerationRef.current,
    }
    cancelRequestRef.current = true
    setCancellingRun(true)
    try {
      await cancelRun(activeRun.runId, activeRun.lastSequence)
    } catch (cancelError) {
      if (
        !ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        }) ||
        activeRunRef.current?.runId !== activeRun.runId
      )
        return
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
    const owner = {
      sessionId: activeSessionIdRef.current,
      generation: runGenerationRef.current,
    }
    const isCurrent = () =>
      ownsRun(owner, {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
      }) && activeRunRef.current?.runId === activeRun.runId
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
      if (isCurrent() && approvalRequestRef.current === toolCallId) {
        setApprovalError(visibleError(responseError))
        setApprovalPendingToolId((current) =>
          current === toolCallId ? null : current
        )
      }
    } finally {
      if (isCurrent() && approvalRequestRef.current === toolCallId)
        approvalRequestRef.current = null
    }
  }

  const submitComposer = async () => {
    const content = composerValue.trim()
    if (!content || pending || sessionLoading || submissionLockRef.current)
      return
    const model = settings?.models?.find((item) => item.id === selectedModelId)
    const provider = settings?.providers.find(
      (item) => item.id === model?.provider_id
    )
    const configured = provider?.configured ?? model?.configured ?? false
    if (!model || (model.requires_api_key && !configured)) {
      setError(
        model
          ? "The selected model is unavailable. Choose a ready-to-use model in Settings and try again."
          : "Open Settings before starting a session."
      )
      return
    }

    submissionLockRef.current = true
    setPending(true)
    setComposerValue("")
    let preservePending = false
    let owner = {
      sessionId: activeSessionIdRef.current,
      generation: runGenerationRef.current,
    }
    const isCurrent = () =>
      ownsRun(owner, {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
      })
    try {
      let session = activeSession
      if (!session) {
        const createdSession = await api.createSession()
        if (!isCurrent()) return
        session = createdSession
        sessionLoadSequenceRef.current += 1
        activeSessionIdRef.current = createdSession.id
        setActiveSession(createdSession)
        setSessions((current) => upsertSession(current, createdSession))
        setActiveView("session")
      }
      const completion = completeTurn(
        {
          sessionId: session.id,
          turnId: crypto.randomUUID(),
          content,
          modelId: model.id,
          budgetPreset: selectedBudgetPreset,
        },
        true
      )
      owner = {
        sessionId: session.id,
        generation: runGenerationRef.current,
      }
      preservePending = await completion
    } catch (submitError) {
      if (!isCurrent()) return
      setComposerValue(content)
      setError(visibleError(submitError))
    } finally {
      if (isCurrent()) {
        submissionLockRef.current = false
        if (!preservePending) setPending(false)
      }
    }
  }

  const retryFailedTurn = async () => {
    if (!failedTurn || pending || submissionLockRef.current) return
    submissionLockRef.current = true
    setPending(true)
    let preservePending = false
    const completion = completeTurn(failedTurn, false)
    const owner = {
      sessionId: failedTurn.sessionId,
      generation: runGenerationRef.current,
    }
    try {
      preservePending = await completion
    } finally {
      if (
        ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      ) {
        submissionLockRef.current = false
        if (!preservePending) setPending(false)
      }
    }
  }

  const reconnectRestoredRun = () => {
    const sessionId = activeSessionIdRef.current
    if (!sessionId || !reconnectAvailable) return
    setReconnectAvailable(false)
    setError("Reconnecting to Trellis…")
    let generation = runGenerationRef.current
    const recovery = (async () => {
      const detail = await api.getSession(sessionId)
      if (
        activeSessionIdRef.current !== sessionId ||
        runGenerationRef.current !== generation
      )
        return
      setSessions((current) => upsertSession(current, detail.session))
      setActiveSession(detail.session)
      setMessages(detail.messages)
      setAssistantOutputState((current) =>
        mergeSavedAssistantOutputs(current, detail.assistant_outputs ?? [])
      )
      const following = restoreSessionRun(
        sessionId,
        detail.messages,
        () => activeSessionIdRef.current !== sessionId
      )
      generation = runGenerationRef.current
      await following
    })()
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

  const renderedAccountControl =
    typeof accountControl === "function"
      ? accountControl(profile)
      : accountControl

  if (bootstrap.status === "loading" || bootstrap.status === "failed") {
    return (
      <main
        className="onboarding-shell onboarding-loading-shell"
        data-theme="dark"
      >
        {renderedAccountControl && (
          <header className="onboarding-topbar onboarding-account-loading">
            {renderedAccountControl}
          </header>
        )}
        {bootstrap.status === "failed" ? (
          <section
            className="workspace-bootstrap-error"
            aria-labelledby="workspace-bootstrap-heading"
          >
            <h1 id="workspace-bootstrap-heading">Your chats could not open</h1>
            <p role="alert">{bootstrap.message}</p>
            <button
              className="onboarding-primary-action"
              type="button"
              onClick={() => {
                setBootstrap({ status: "loading" })
                void restoreWorkspace()
              }}
            >
              Try again
            </button>
          </section>
        ) : (
          <div className="workspace-loading" role="status">
            Loading your chats…
          </div>
        )}
      </main>
    )
  }

  if (bootstrap.status === "onboarding" && profile && settings) {
    return (
      <OnboardingFlow
        accountControl={renderedAccountControl}
        profile={profile}
        settings={settings}
        initialStep={
          bootstrap.progress.current_step === "complete"
            ? "intro"
            : bootstrap.progress.current_step
        }
        onAdvanceIntro={advanceOnboardingIntro}
        onSaveProfile={saveOnboardingProfile}
        onRetryModels={restoreWorkspace}
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
          accountControl={renderedAccountControl}
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
              onboarding={
                bootstrap.status === "ready" ? bootstrap.progress : undefined
              }
              onProfileChange={setProfile}
              onSettingsChange={setSettings}
            />
          ) : activeView === "session" && activeSession ? (
            <ChatThread
              session={activeSession}
              messages={messages}
              assistantOutputs={assistantOutputState.outputs}
              pending={pending}
              streamingText={streamingText}
              error={error}
              activityHistoryError={activityHistoryError}
              onRetryActivityHistory={() =>
                void selectSession(activeSession.id)
              }
              canRetry={failedTurn !== null && !pending}
              canReconnect={reconnectAvailable}
              canCancel={pending && activeRunId !== null}
              cancellationPending={cancellingRun}
              runActivities={runActivities}
              onRequestActivityEvents={loadTurnEvents}
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
              budgetPreset={selectedBudgetPreset}
              onBudgetChange={setRunBudgetChoice}
            />
          </>
        ) : null}
      </section>
    </main>
  )
}
