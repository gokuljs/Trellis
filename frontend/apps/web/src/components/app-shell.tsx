import { Menu } from "lucide-react"
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import {
  useInfiniteQuery,
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { ChatThread } from "@/components/chat-thread"
import { Composer } from "@/components/composer"
import { ContextGauge } from "@/components/context-gauge"
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
  newAssistantOutputState,
  reduceAssistantOutputEvent,
} from "@/lib/assistant-outputs"
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
  OnboardingState,
  Profile,
  Session,
  RunSummary,
  SessionHistoryPage,
  TimelineRow,
  Settings,
  WorkspaceView,
} from "@/lib/app-types"
import {
  ownsRun,
  recoveryTranscript,
  turnFailureAction,
  type TerminalRunOutcome,
} from "@/lib/run-recovery"
import {
  ensureSessionModel,
  isModelSelectable,
  ownsModelSelection,
  selectedChatModelId,
  type ModelSelectionOwner,
} from "@/lib/model-choice"
import {
  cacheSession,
  resetHistoryToLatest,
  sessionHistoryQueryOptions,
  sessionPagesQueryOptions,
  runSummariesQueryOptions,
  serverKeys,
  sessionModelMutationOptions,
  sessionModelMutationFilters,
  settingsQueryOptions,
} from "@/lib/server-queries"
import {
  activeRunContext,
  newRunContextState,
  reduceRunContext,
} from "@/lib/context-gauge"
import {
  flattenHistoryPages,
  hydrateOutputWindow,
  retryInputForRun,
} from "@/lib/history-window"
import {
  activitiesFromSummaries,
  startNextAttempt,
  retainWindowActivities,
} from "@/lib/activity-history"
import { useContextPreview } from "@/lib/use-context-preview"
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

export function AppShell({
  accountControl,
}: {
  accountControl?: ReactNode | ((profile: Profile | null) => ReactNode)
} = {}) {
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [draftModelId, setDraftModelId] = useState<string | null>(null)
  const [modelSelection, setModelSelection] = useState<{
    sessionId: string
    modelId: string
  } | null>(null)
  const [runBudgetChoice, setRunBudgetChoice] = useState<
    "conservative" | "longer" | null
  >(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [bootstrap, setBootstrap] = useState<BootstrapState>({
    status: "loading",
  })
  const queryClient = useQueryClient()
  const { data: settings = null } = useQuery({
    ...settingsQueryOptions(),
    enabled: bootstrap.status === "ready" || bootstrap.status === "onboarding",
  })
  const setSettings = useCallback(
    (next: Settings) => queryClient.setQueryData(serverKeys.settings, next),
    [queryClient]
  )
  const { mutateAsync: selectSessionModel } = useMutation(
    sessionModelMutationOptions(queryClient)
  )
  const [activeSession, setActiveSession] = useState<Session | null>(null)
  const sessionModelSaving =
    useIsMutating(sessionModelMutationFilters(activeSession?.id ?? null)) > 0
  const modelSaving = modelSelection !== null || sessionModelSaving
  const [messages, setMessages] = useState<Message[]>([])
  const [timelineOrder, setTimelineOrder] = useState<TimelineRow[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)
  const sessionPages = useInfiniteQuery({
    ...sessionPagesQueryOptions(),
    enabled: bootstrap.status === "ready",
  })
  const sessions = [
    ...new Map(
      sessionPages.data?.pages
        .flatMap((page) => page.items)
        .map((session) => [session.id, session]) ?? []
    ).values(),
  ]
  const history = useInfiniteQuery({
    ...sessionHistoryQueryOptions(activeSession?.id ?? ""),
    enabled: false,
  })
  const [assistantOutputState, setAssistantOutputState] = useState(
    newAssistantOutputState
  )
  const [runContextState, setRunContextState] = useState(newRunContextState)
  const [contextGeneration, setContextGeneration] = useState(0)
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
  const modelRequestSequenceRef = useRef(0)
  const modelSaveOwnerRef = useRef<ModelSelectionOwner | null>(null)
  const activeRunRef = useRef<{ runId: string; lastSequence: number } | null>(
    null
  )
  const runGenerationRef = useRef(0)
  const cancelRequestRef = useRef(false)
  const approvalRequestRef = useRef<string | null>(null)
  const bootstrapControllerRef = useRef<AbortController | null>(null)

  const applyHistory = useCallback(
    (detail: SessionHistoryPage, keepTurnId?: string) => {
      cacheSession(queryClient, detail.session)
      setActiveSession(detail.session)
      setMessages(detail.messages)
      setTimelineOrder(detail.timeline_order)
      setHistoryError(null)
      setAssistantOutputState((current) =>
        hydrateOutputWindow(
          current,
          detail.assistant_outputs ?? [],
          activeRunRef.current?.runId
        )
      )
      const turns = new Set(
        [...detail.messages, ...(detail.assistant_outputs ?? [])].map(
          (row) => row.turn_id
        )
      )
      setRunActivities((current) =>
        retainWindowActivities(
          current,
          turns,
          activeRunRef.current?.runId,
          keepTurnId
        )
      )
    },
    [queryClient]
  )

  const loadLatestHistory = useCallback(
    async (sessionId: string) =>
      flattenHistoryPages(
        (await resetHistoryToLatest(queryClient, sessionId)).pages
      ),
    [queryClient]
  )

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
      setRunContextState((current) => reduceRunContext(current, event))
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

  const restoreSessionRun = useCallback(
    async (
      sessionId: string,
      transcript: Message[],
      isCancelled: () => boolean,
      ownerGeneration?: number
    ) => {
      const generation = ownerGeneration ?? ++runGenerationRef.current
      setContextGeneration(generation)
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
      let turn = retryFromTranscript(sessionId, transcript)
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
      if (latest && (!turn || turn.turnId === latest.turn_id)) {
        turn = retryInputForRun(sessionId, transcript, latest)
        if (!turn && activeRunStatuses.has(latest.status)) {
          waitForDiscovery(
            "Trellis could not load the active run's input. Reconnect to try again."
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
          const detail = await loadLatestHistory(sessionId)
          if (isStale()) return
          applyHistory(detail)
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
            const detail = await loadLatestHistory(sessionId)
            if (isStale()) return
            applyHistory(detail)
            if (!keepActive && currentRun?.status === "completed") {
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
    [applyHistory, loadLatestHistory, recordRunEvent, recordRunInfo]
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
      const [restoredProfile, , restoredOnboarding] = await Promise.all([
        api.getProfile(options),
        queryClient.fetchQuery({
          ...settingsQueryOptions(),
          staleTime: 0,
          queryFn: ({ signal }) =>
            api.getSettings({
              signal: AbortSignal.any([signal, controller.signal]),
            }),
        }),
        api.getOnboarding(options),
      ])
      if (!current()) return

      setProfile(restoredProfile)
      if (!restoredOnboarding.completed) {
        setBootstrap({ status: "onboarding", progress: restoredOnboarding })
        return
      }
      await queryClient.fetchInfiniteQuery({
        ...sessionPagesQueryOptions(),
        staleTime: 0,
        queryFn: ({ pageParam, signal }) =>
          api.listSessionPages(pageParam, {
            signal: AbortSignal.any([signal, controller.signal]),
          }),
      })
      if (!current()) return
      setBootstrap({ status: "ready", progress: restoredOnboarding })
    } catch (restoreError) {
      if (current()) {
        setBootstrap({ status: "failed", message: visibleError(restoreError) })
        controller.abort()
      }
    } finally {
      clearTimeout(timer)
    }
  }, [queryClient])

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
    setContextGeneration(runGenerationRef.current)
    submissionLockRef.current = false
    modelRequestSequenceRef.current += 1
    modelSaveOwnerRef.current = null
    setModelSelection(null)
    setDraftModelId(null)
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = null
    setComposerValue("")
    setActiveSession(null)
    setMessages([])
    setTimelineOrder([])
    setHistoryError(null)
    setAssistantOutputState(newAssistantOutputState())
    setRunContextState(newRunContextState())
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
    setContextGeneration(runGenerationRef.current)
    modelRequestSequenceRef.current += 1
    modelSaveOwnerRef.current = null
    setModelSelection(null)
    const loadSequence = sessionLoadSequenceRef.current + 1
    sessionLoadSequenceRef.current = loadSequence
    activeSessionIdRef.current = sessionId
    setSidebarOpen(false)
    setActiveView("session")
    setActiveSession(
      sessions.find((session) => session.id === sessionId) ?? null
    )
    setMessages([])
    setTimelineOrder([])
    setHistoryError(null)
    setAssistantOutputState(newAssistantOutputState())
    setRunContextState(newRunContextState())
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
    setActivityHistoryError(null)
    setFailedTurn(null)
    try {
      const [detail, history] = await Promise.all([
        loadLatestHistory(sessionId),
        queryClient
          .fetchInfiniteQuery({
            ...runSummariesQueryOptions(sessionId),
            staleTime: 0,
          })
          .then((data) => data.pages.flatMap((page) => page.items))
          .then(
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
      applyHistory(detail)
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

  const loadOlderHistory = async () => {
    if (pending || history.isFetching || !activeSession) return
    const owner = {
      sessionId: activeSession.id,
      generation: runGenerationRef.current,
    }
    try {
      const result = await history.fetchNextPage()
      if (
        !ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      )
        return
      if (result.error) throw result.error
      if (result.data) applyHistory(flattenHistoryPages(result.data.pages))
    } catch (pageError) {
      if (
        ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      )
        setHistoryError(visibleError(pageError))
    }
  }

  const jumpToLatest = async () => {
    if (pending || history.isFetching || !activeSession) return
    const owner = {
      sessionId: activeSession.id,
      generation: runGenerationRef.current,
    }
    try {
      const detail = await loadLatestHistory(activeSession.id)
      if (
        ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      )
        applyHistory(detail)
    } catch (pageError) {
      if (
        ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      )
        setHistoryError(visibleError(pageError))
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
    setContextGeneration(owner.generation)
    const isCurrent = () =>
      ownsRun(owner, {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
      })
    setError(null)
    setRunContextState(newRunContextState())
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
      const newest = await loadLatestHistory(turn.sessionId)
      if (!isCurrent()) return true
      applyHistory(newest, turn.turnId)
      if (optimistic) setMessages(recoveryTranscript(newest.messages, turn))
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
      const detail = await loadLatestHistory(turn.sessionId)
      if (!isCurrent()) return true
      cacheSession(queryClient, detail.session)
      if (activeSessionIdRef.current === turn.sessionId) {
        setActiveSession(detail.session)
        applyHistory(detail)
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
          const detail = await loadLatestHistory(turn.sessionId)
          if (!isCurrent()) return true
          transcript = detail.messages
          applyHistory(detail)
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
        const detail = await loadLatestHistory(turn.sessionId)
        if (!isCurrent()) return true
        cacheSession(queryClient, detail.session)
        if (activeSessionIdRef.current === turn.sessionId) {
          setActiveSession(detail.session)
          applyHistory(detail)
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

  const saveSessionModel = async (
    sessionId: string,
    modelId: string,
    generation: number
  ): Promise<Session | null> => {
    const owner = {
      sessionId,
      generation,
      requestSequence: ++modelRequestSequenceRef.current,
    }
    const isCurrent = () =>
      ownsModelSelection(owner, {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
        requestSequence: modelRequestSequenceRef.current,
      })
    modelSaveOwnerRef.current = owner
    setModelSelection({ sessionId, modelId })
    try {
      const updated = await selectSessionModel({ sessionId, modelId })
      if (!isCurrent()) return null
      cacheSession(queryClient, updated)
      setActiveSession(updated)
      return updated
    } catch (saveError) {
      if (!isCurrent()) return null
      throw saveError
    } finally {
      if (isCurrent()) {
        modelSaveOwnerRef.current = null
        setModelSelection(null)
      }
    }
  }

  const changeChatModel = async (modelId: string) => {
    if (
      pending ||
      sessionLoading ||
      submissionLockRef.current ||
      modelSaveOwnerRef.current ||
      sessionModelSaving
    )
      return
    if (
      !isModelSelectable(
        settings?.models?.find((model) => model.id === modelId)
      )
    )
      return
    setError(null)
    if (!activeSession) {
      setDraftModelId(modelId)
      return
    }
    if (selectedModelId === modelId) return
    const owner = {
      sessionId: activeSession.id,
      generation: runGenerationRef.current,
    }
    try {
      await saveSessionModel(activeSession.id, modelId, owner.generation)
    } catch (saveError) {
      if (
        ownsRun(owner, {
          sessionId: activeSessionIdRef.current,
          generation: runGenerationRef.current,
        })
      )
        setError(visibleError(saveError))
    }
  }

  const submitComposer = async () => {
    const content = composerValue.trim()
    if (
      !content ||
      pending ||
      sessionLoading ||
      submissionLockRef.current ||
      modelSaveOwnerRef.current ||
      sessionModelSaving
    )
      return
    const model = settings?.models?.find((item) => item.id === selectedModelId)
    if (!model || !isModelSelectable(model)) {
      setError(
        model
          ? "The selected model is unavailable. Choose a ready-to-use model for this chat and try again."
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
        owner = { ...owner, sessionId: createdSession.id }
        setActiveSession(createdSession)
        cacheSession(queryClient, createdSession)
        setActiveView("session")
        const prepared = await ensureSessionModel(
          createdSession,
          model.id,
          (sessionId, modelId) =>
            saveSessionModel(sessionId, modelId, owner.generation)
        )
        if (!isCurrent() || !prepared) return
        session = prepared
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
    if (
      !failedTurn ||
      pending ||
      sessionLoading ||
      submissionLockRef.current ||
      modelSaveOwnerRef.current ||
      sessionModelSaving
    )
      return
    if (
      !isModelSelectable(
        settings?.models?.find((model) => model.id === selectedModelId)
      )
    ) {
      setError("Choose a ready-to-use model for this chat before retrying.")
      return
    }
    submissionLockRef.current = true
    setPending(true)
    let preservePending = false
    const completion = completeTurn(
      { ...failedTurn, modelId: selectedModelId },
      false
    )
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
      const detail = await loadLatestHistory(sessionId)
      if (
        activeSessionIdRef.current !== sessionId ||
        runGenerationRef.current !== generation
      )
        return
      applyHistory(detail)
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

  const selectedModelId =
    (modelSelection?.sessionId === activeSession?.id
      ? modelSelection?.modelId
      : null) ??
    selectedChatModelId(
      activeSession,
      settings,
      draftModelId,
      history.data?.pages[0]?.session ?? null
    )
  const selectedBudgetPreset =
    runBudgetChoice ?? settings?.default_budget_preset ?? "conservative"
  const activeActivity = activeRunTurnId ? runActivities[activeRunTurnId] : null
  const lockedModelId = pending
    ? (activeActivity?.runInfo?.modelId ??
      (runContextState.runId === activeRunId
        ? runContextState.preview?.model_id
        : null) ??
      selectedModelId)
    : selectedModelId
  const liveContext = pending
    ? activeRunContext(runContextState, activeRunId, lockedModelId)
    : null
  const compacting =
    pending &&
    runContextState.runId === activeRunId &&
    runContextState.compacting
  const contextPreview = useContextPreview(
    {
      ...(activeSession ? { session_id: activeSession.id } : {}),
      model_id: selectedModelId,
      draft: composerValue,
      budget_preset: selectedBudgetPreset,
    },
    contextGeneration,
    bootstrap.status === "ready" &&
      activeView !== "Settings" &&
      !pending &&
      !sessionLoading &&
      !modelSaving &&
      Boolean(selectedModelId)
  )

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
          hasOlderSessions={Boolean(sessionPages.hasNextPage)}
          browsingSessions={Boolean(
            sessionPages.data &&
            (sessionPages.data.pages.length > 1 ||
              sessionPages.data.pageParams[0] !== null)
          )}
          sessionsLoading={sessionPages.isFetching}
          sessionsError={
            sessionPages.error ? "Could not load older chats. Try again." : null
          }
          onOlderSessions={() => void sessionPages.fetchNextPage()}
          onRecentSessions={() =>
            void queryClient.resetQueries({
              queryKey: serverKeys.sessionPages,
              exact: true,
            })
          }
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
              timelineOrder={timelineOrder}
              hasOlderHistory={Boolean(history.hasNextPage)}
              browsingHistory={Boolean(
                history.data &&
                (history.data.pages.length > 1 ||
                  history.data.pageParams[0] !== null)
              )}
              historyLoading={history.isFetching}
              historyError={historyError}
              onLoadOlderHistory={() => void loadOlderHistory()}
              onJumpToLatest={() => void jumpToLatest()}
              pending={pending}
              streamingText={streamingText}
              error={error}
              activityHistoryError={activityHistoryError}
              onRetryActivityHistory={() =>
                void selectSession(activeSession.id)
              }
              canRetry={failedTurn !== null && !pending && !modelSaving}
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
            <Composer
              value={composerValue}
              placeholder={
                activeView === "New session"
                  ? "What are we building?"
                  : "Adjust or continue"
              }
              onChange={setComposerValue}
              onSubmit={() => void submitComposer()}
              disabled={pending || sessionLoading || modelSaving}
              budgetPreset={selectedBudgetPreset}
              onBudgetChange={setRunBudgetChoice}
              models={settings?.models ?? []}
              selectedModelId={selectedModelId}
              modelSaving={modelSaving}
              modelReady={isModelSelectable(
                settings?.models?.find((model) => model.id === selectedModelId)
              )}
              onModelChange={(modelId) => void changeChatModel(modelId)}
              contextGauge={
                <ContextGauge
                  preview={pending ? liveContext : contextPreview.preview}
                  modelName={
                    settings?.models?.find(
                      (model) => model.id === lockedModelId
                    )?.name ?? lockedModelId
                  }
                  loading={
                    pending
                      ? !liveContext && !compacting
                      : contextPreview.loading || sessionLoading || modelSaving
                  }
                  error={pending ? null : contextPreview.error}
                  compacting={compacting}
                />
              }
            />
          </>
        ) : null}
      </section>
    </main>
  )
}
