import { Menu } from "lucide-react"
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"
import {
  ServiceReadinessError,
  waitUntilServiceReady,
} from "@/lib/service-readiness"
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
  chatPath,
  currentPath,
  navigate,
  parseAppRoute,
  subscribeToPath,
  useAppPath,
} from "@/lib/app-routes"
import { coalesceSummaryRefresh } from "@/lib/activity-summary"
import {
  newAssistantOutputState,
  reduceAssistantOutputEvent,
} from "@/lib/assistant-outputs"
import {
  RuntimeError,
  cancelRun,
  lookupRun,
  resumeRun,
  respondToToolApproval,
  clarifyResearch,
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
  WorkspaceReviewStatus,
  RunMode,
} from "@/lib/app-types"
import {
  ownsRun,
  isReconciledRunOutcome,
  recoveryTranscript,
  turnFailureAction,
  recoveryRunInfo,
  mergeRunInfo,
  runInfoFromEvent,
  allowsRunRetry,
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
import { logFailure } from "@/lib/logging"
import { getAuthController } from "@/lib/auth-controller"
import {
  clearPendingRun,
  readPendingRun,
  savePendingRun,
} from "@/lib/pending-run"
import "./workspace-bootstrap.css"
import { researchQueryOptions } from "@/lib/research"
import "./research.css"

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
  runMode?: RunMode
}

function visibleError(error: unknown) {
  return error instanceof ApiError ||
    error instanceof RuntimeError ||
    error instanceof ServiceReadinessError
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
  "recovering",
  "waiting_for_approval",
  "waiting_for_input",
  "cancelling",
])
const maxRestoredReconnectAttempts = 4

export function AppShell({
  accountControl,
}: {
  accountControl?: ReactNode | ((profile: Profile | null) => ReactNode)
} = {}) {
  const path = useAppPath()
  const route = parseAppRoute(path)
  const appliedPathRef = useRef<string | null>(null)
  const [activeView, setActiveView] = useState<WorkspaceView>("New session")
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [composerValue, setComposerValue] = useState("")
  const [runMode, setRunMode] = useState<RunMode>("chat")
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false)
  const clarificationRequestRef = useRef<{
    runId: string
    clarificationId: string
    text: string
    clientRequestId: string
  } | null>(null)
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
  const [latestRun, setLatestRun] = useState<LatestRun | null>(null)
  const [workspaceReview, setWorkspaceReview] =
    useState<WorkspaceReviewStatus | null>(null)
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
  const workspaceReviewRequestRef = useRef(0)
  const bootstrapControllerRef = useRef<AbortController | null>(null)
  const runConnectionControllerRef = useRef<AbortController | null>(null)
  const summaryRefresh = useMemo(
    () =>
      coalesceSummaryRefresh((sessionId) => {
        void queryClient.invalidateQueries({
          queryKey: ["run-summaries", sessionId],
        })
      }),
    [queryClient]
  )
  useEffect(() => () => summaryRefresh.dispose(), [summaryRefresh])

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

  const refreshWorkspaceReview = useCallback(async (sessionId: string) => {
    const generation = runGenerationRef.current
    const requestId = ++workspaceReviewRequestRef.current
    try {
      const saved = await api.getWorkspaceReview(sessionId)
      if (
        activeSessionIdRef.current === sessionId &&
        runGenerationRef.current === generation &&
        workspaceReviewRequestRef.current === requestId
      )
        setWorkspaceReview(saved)
    } catch {
      if (
        activeSessionIdRef.current === sessionId &&
        runGenerationRef.current === generation &&
        workspaceReviewRequestRef.current === requestId
      )
        setWorkspaceReview({
          blocked: true,
          reason: "workspace_review_unavailable",
        })
    }
  }, [])

  const recordRunInfo = useCallback(
    (turnId: string, runInfo: RuntimeRunInfo) => {
      setLatestRun((current) => {
        const merged = mergeRunInfo(
          current ? recoveryRunInfo(current) : null,
          runInfo
        )
        return {
          ...(current?.run_id === runInfo.runId ? current : {}),
          run_id: merged.runId,
          turn_id: turnId,
          status: merged.status ?? "running",
          run_mode: merged.runMode,
          last_sequence:
            current?.run_id === runInfo.runId ? current.last_sequence : 0,
          retry_allowed: merged.retryAllowed,
          recovery_reason: merged.recoveryReason,
          uncertain_tool_call_id: merged.uncertainToolCallId,
        }
      })
      setRunActivities((current) => ({
        ...current,
        [turnId]: {
          ...current[turnId],
          events: current[turnId]?.events ?? [],
          runInfo: mergeRunInfo(current[turnId]?.runInfo, runInfo),
          latestEvent: current[turnId]?.latestEvent,
        },
      }))
    },
    []
  )

  const recordRunEvent = useCallback(
    (turnId: string, event: RuntimeRunEvent) => {
      const userId = getAuthController().getSnapshot().user?.id
      const sessionId = activeSessionIdRef.current
      if (userId && sessionId) {
        const saved = readPendingRun(userId, sessionId)
        if (saved?.runId === event.runId) {
          if (
            [
              "run.completed",
              "run.failed",
              "run.cancelled",
              "run.interrupted",
            ].includes(event.eventType)
          )
            clearPendingRun(userId, sessionId, saved.clientRequestId)
          else savePendingRun({ ...saved, lastSequence: event.sequence })
        }
      }
      if (activeSessionIdRef.current)
        summaryRefresh.schedule(activeSessionIdRef.current, event)
      if (
        sessionId &&
        (event.eventType.startsWith("research.") ||
          event.eventType === "run.phase" ||
          event.eventType === "run.completed")
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["research", sessionId, event.runId],
        })
      }
      setRunContextState((current) => reduceRunContext(current, event))
      setAssistantOutputState((current) =>
        reduceAssistantOutputEvent(current, event, turnId)
      )
      setLatestRun((current) => {
        if (current?.run_id !== event.runId) return current
        const info = runInfoFromEvent(recoveryRunInfo(current), event)
        return {
          ...current,
          status: info.status ?? current.status,
          run_mode: info.runMode ?? current.run_mode,
          last_sequence: Math.max(current.last_sequence, event.sequence),
          retry_allowed: info.retryAllowed,
          recovery_reason: info.recoveryReason,
          uncertain_tool_call_id: info.uncertainToolCallId,
        }
      })
      if (
        sessionId &&
        [
          "run.completed",
          "run.failed",
          "run.cancelled",
          "run.interrupted",
        ].includes(event.eventType)
      ) {
        const generation = runGenerationRef.current
        void refreshWorkspaceReview(sessionId)
        void api
          .getLatestRun(sessionId)
          .then((saved) => {
            if (
              activeSessionIdRef.current !== sessionId ||
              runGenerationRef.current !== generation ||
              saved?.run_id !== event.runId
            )
              return
            setLatestRun((current) =>
              current?.run_id === saved.run_id &&
              current.last_sequence > saved.last_sequence
                ? current
                : saved
            )
          })
          .catch(() => undefined)
      }
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
          [turnId]: {
            ...activity,
            events,
            latestEvent: event,
            runInfo: runInfoFromEvent(activity.runInfo, event),
          },
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
    [summaryRefresh, refreshWorkspaceReview, queryClient]
  )

  const restoreSessionRun = useCallback(
    async (
      sessionId: string,
      transcript: Message[],
      isCancelled: () => boolean,
      ownerGeneration?: number
    ) => {
      const existingConnection = runConnectionControllerRef.current
      const connection =
        ownerGeneration !== undefined &&
        existingConnection &&
        !existingConnection.signal.aborted
          ? existingConnection
          : new AbortController()
      if (existingConnection !== connection) existingConnection?.abort()
      runConnectionControllerRef.current = connection
      const generation = ownerGeneration ?? ++runGenerationRef.current
      setContextGeneration(generation)
      const isStale = () =>
        connection.signal.aborted ||
        !bootstrapControllerRef.current ||
        isCancelled() ||
        runGenerationRef.current !== generation
      setPending(true)
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
      void refreshWorkspaceReview(sessionId)
      let turn = retryFromTranscript(sessionId, transcript)
      let latest: LatestRun | null
      let submissionMissing: boolean
      const userId = getAuthController().getSnapshot().user?.id
      const saved = userId ? readPendingRun(userId, sessionId) : null
      if (turn && saved?.runMode) turn = { ...turn, runMode: saved.runMode }
      try {
        const exact = saved
          ? await lookupRun(sessionId, saved.clientRequestId)
          : null
        if (isStale()) return
        if (saved?.runId && exact && exact.runId !== saved.runId)
          throw new RuntimeError(
            "runtime_protocol_error",
            "The saved request returned a different run."
          )
        latest = await api.getLatestRun(sessionId)
        if (isStale()) return
        setLatestRun(latest)
        submissionMissing =
          !!saved && !exact && latest?.turn_id !== saved.turnId
        if (exact && !latest)
          throw new RuntimeError(
            "runtime_protocol_error",
            "The accepted run could not be loaded."
          )
        if (saved && userId) {
          if (!exact || !activeRunStatuses.has(exact.status ?? ""))
            clearPendingRun(userId, sessionId, saved.clientRequestId)
          else savePendingRun({ ...saved, runId: exact.runId })
        }
      } catch (discoveryError) {
        if (isStale()) return
        logFailure(discoveryError, "run.restore")
        waitForDiscovery(
          "Trellis could not check the previous run. Reconnect when the service returns."
        )
        return
      }
      if (isStale()) return
      if (
        latest &&
        (!submissionMissing || activeRunStatuses.has(latest.status))
      ) {
        turn = retryInputForRun(sessionId, transcript, latest)
        if (!turn && activeRunStatuses.has(latest.status)) {
          logFailure(
            new RuntimeError(
              "runtime_protocol_error",
              "The active run input is unavailable."
            ),
            "run.restore"
          )
          waitForDiscovery(
            "Trellis could not load the active run's input. Reconnect to try again."
          )
          return
        }
      }
      if (!latest) {
        if (submissionMissing) setMessages(transcript)
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
        let historyReloadFailed = false
        let summary: RunSummary | undefined
        if (submissionMissing) setMessages(transcript)
        else {
          try {
            const detail = await loadLatestHistory(sessionId)
            if (isStale()) return
            applyHistory(detail)
          } catch (historyError) {
            if (isStale()) return
            historyReloadFailed = true
            logFailure(historyError, "history.load")
          }
        }
        try {
          const summaries = await queryClient.fetchInfiniteQuery({
            ...runSummariesQueryOptions(sessionId, undefined, latest.turn_id),
            staleTime: 0,
          })
          if (isStale()) return
          summary = summaries.pages
            .flatMap((page) => page.items)
            .find(
              (item) =>
                item.run_id === latest.run_id &&
                item.status === latest.status &&
                item.last_sequence >= latest.last_sequence
            )
          if (!summary)
            throw new RuntimeError(
              "runtime_protocol_error",
              "The finished run's saved activity could not be loaded."
            )
        } catch (summaryError) {
          if (isStale()) return
          historyReloadFailed = true
          logFailure(summaryError, "activity.summaries")
        }
        setRunActivities((current) => {
          if (isStale()) return current
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
              events: aligned?.events ?? [],
              runInfo: mergeRunInfo(aligned?.runInfo, recoveryRunInfo(latest)),
              ...(summary ? { summary } : {}),
            },
          }
        })
        if (userId && saved)
          clearPendingRun(userId, sessionId, saved.clientRequestId)
        setPending(false)
        setStreamingText(null)
        setActiveRunId(null)
        setActiveRunTurnId(null)
        activeRunRef.current = null
        setReconnectAvailable(historyReloadFailed)
        setCancellingRun(false)
        if (historyReloadFailed) {
          setFailedTurn(null)
          setError(
            "The run has finished, but some saved details could not be loaded. Reconnect to try again."
          )
        } else if (
          turn &&
          (submissionMissing || latest.status !== "completed")
        ) {
          setFailedTurn(turn)
          setError(
            submissionMissing
              ? "Your request was not accepted. Retry when you are ready."
              : "The previous assistant response did not complete."
          )
        } else {
          setFailedTurn(null)
          setError(null)
        }
        return
      }

      setFailedTurn(null)
      setError(null)
      if (turn) setMessages(recoveryTranscript(transcript, turn))
      setPending(true)
      setCancellingRun(latest.status === "cancelling")
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
            runInfo: mergeRunInfo(aligned?.runInfo, recoveryRunInfo(latest)),
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
                  signal: connection.signal,
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
              if (isStale()) return
              reconnectAttempts += 1
              if (reconnectAttempts >= maxRestoredReconnectAttempts) {
                keepActive =
                  currentStatus === null || activeRunStatuses.has(currentStatus)
                if (keepActive) {
                  logFailure(streamError, "run.restore")
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
          let finalError = restoreError
          let reconciled = false
          try {
            const currentRun = await api.getLatestRun(sessionId)
            if (isStale()) return
            setLatestRun(currentRun)
            keepActive =
              currentRun?.run_id === latest.run_id &&
              activeRunStatuses.has(currentRun.status)
            setReconnectAvailable(keepActive)
            if (!keepActive && userId && saved)
              clearPendingRun(userId, sessionId, saved.clientRequestId)
            if (!keepActive && turn) setFailedTurn(turn)
            const detail = await loadLatestHistory(sessionId)
            if (isStale()) return
            applyHistory(detail)
            if (!keepActive && currentRun?.status === "completed") {
              setFailedTurn(null)
              setError(null)
            }
            reconciled =
              !keepActive && isReconciledRunOutcome(currentRun?.status)
          } catch (reconcileError) {
            if (isStale()) return
            finalError = reconcileError
            keepActive = true
            setReconnectAvailable(true)
            // The run may still be active while the service is unavailable.
          }
          if (!isStale() && !reconciled) logFailure(finalError, "run.restore")
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
    [
      applyHistory,
      loadLatestHistory,
      queryClient,
      recordRunEvent,
      recordRunInfo,
      refreshWorkspaceReview,
    ]
  )

  const restoreWorkspace = useCallback(async () => {
    bootstrapControllerRef.current?.abort()
    const controller = new AbortController()
    bootstrapControllerRef.current = controller
    const options = { signal: controller.signal }
    let timer: ReturnType<typeof setTimeout> | undefined
    const current = () => bootstrapControllerRef.current === controller
    try {
      await waitUntilServiceReady(controller.signal)
      timer = setTimeout(
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
        logFailure(restoreError, "workspace.bootstrap")
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
      sessionLoadSequenceRef.current += 1
      runGenerationRef.current += 1
      activeSessionIdRef.current = null
      runConnectionControllerRef.current?.abort()
      runConnectionControllerRef.current = null
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

  const resetChatState = useCallback((sessionId: string | null) => {
    runConnectionControllerRef.current?.abort()
    runConnectionControllerRef.current = null
    runGenerationRef.current += 1
    setContextGeneration(runGenerationRef.current)
    submissionLockRef.current = false
    modelRequestSequenceRef.current += 1
    modelSaveOwnerRef.current = null
    setModelSelection(null)
    setDraftModelId(null)
    sessionLoadSequenceRef.current += 1
    activeSessionIdRef.current = sessionId
    setComposerValue("")
    setRunMode("chat")
    setClarificationSubmitting(false)
    clarificationRequestRef.current = null
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
    setLatestRun(null)
    setWorkspaceReview(null)
    workspaceReviewRequestRef.current += 1
    setActiveRunTurnId(null)
    setApprovalPendingToolId(null)
    setApprovalError(null)
    approvalRequestRef.current = null
    setReconnectAvailable(false)
    setCancellingRun(false)
    cancelRequestRef.current = false
    activeRunRef.current = null
    setSessionLoading(sessionId !== null)
    setFailedTurn(null)
    setError(null)
    setActivityHistoryError(null)
    setActiveView(sessionId ? "session" : "New session")
    setSidebarOpen(false)
  }, [])

  const loadSession = useCallback(
    async (sessionId: string) => {
      resetChatState(sessionId)
      const loadSequence = sessionLoadSequenceRef.current
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
              (summaryError: unknown) => {
                if (
                  bootstrapControllerRef.current &&
                  sessionLoadSequenceRef.current === loadSequence &&
                  activeSessionIdRef.current === sessionId
                )
                  logFailure(summaryError, "activity.summaries")
                return {
                  summaries: [] as RunSummary[],
                  error: "Saved activity could not be loaded.",
                }
              }
            ),
        ])
        if (
          !bootstrapControllerRef.current ||
          bootstrapControllerRef.current.signal.aborted ||
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
        if (
          !bootstrapControllerRef.current ||
          bootstrapControllerRef.current.signal.aborted ||
          sessionLoadSequenceRef.current !== loadSequence ||
          activeSessionIdRef.current !== sessionId
        )
          return
        if (
          sessionError instanceof ApiError &&
          [403, 404, 410].includes(sessionError.status)
        ) {
          resetChatState(null)
          setError("This chat is unavailable.")
          appliedPathRef.current = "/app"
          navigate("/app", true)
          return
        }
        if (bootstrapControllerRef.current)
          logFailure(sessionError, "sessions.open")
        setError(visibleError(sessionError))
      } finally {
        if (sessionLoadSequenceRef.current === loadSequence) {
          setSessionLoading(false)
        }
      }
    },
    [
      applyHistory,
      loadLatestHistory,
      queryClient,
      resetChatState,
      restoreSessionRun,
    ]
  )

  useEffect(() => {
    if (bootstrap.status !== "ready") return
    const synchronizeRoute = () => {
      const next = parseAppRoute(currentPath())
      if (next.kind === "unknown") {
        navigate("/app", true)
        return
      }
      if (next.kind !== "workspace" && next.kind !== "chat") return
      if (next.path !== currentPath()) {
        navigate(next.path, true)
        return
      }
      if (appliedPathRef.current === next.path) return
      appliedPathRef.current = next.path
      if (next.kind === "chat") void loadSession(next.chatId)
      else resetChatState(null)
    }
    const unsubscribe = subscribeToPath(synchronizeRoute)
    let mounted = true
    void Promise.resolve().then(() => {
      if (mounted) synchronizeRoute()
    })
    return () => {
      mounted = false
      unsubscribe()
    }
  }, [bootstrap.status, loadSession, resetChatState])

  const handleNavigate = (view: WorkspaceView) => {
    if (view === "New session") {
      if (currentPath() === "/app") resetChatState(null)
      else navigate("/app")
      setSidebarCollapsed(false)
      return
    }
    setActiveView(view)
    setSidebarOpen(false)
  }

  const selectSession = (sessionId: string) => {
    const target = chatPath(sessionId)
    if (currentPath() === target) {
      setActiveView("session")
      setSidebarOpen(false)
    } else navigate(target)
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
      ) {
        if (bootstrapControllerRef.current)
          logFailure(pageError, "history.load")
        setHistoryError(visibleError(pageError))
      }
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
      ) {
        if (bootstrapControllerRef.current)
          logFailure(pageError, "history.load")
        setHistoryError(visibleError(pageError))
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

  const completeTurn = async (
    turn: FailedTurn & { clientRequestId: string },
    optimistic: boolean
  ) => {
    runConnectionControllerRef.current?.abort()
    const connection = new AbortController()
    runConnectionControllerRef.current = connection
    const owner = {
      sessionId: turn.sessionId,
      generation: ++runGenerationRef.current,
    }
    setContextGeneration(owner.generation)
    setLatestRun(null)
    const isCurrent = () =>
      !connection.signal.aborted &&
      !!bootstrapControllerRef.current &&
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
      const userId = getAuthController().getSnapshot().user?.id
      if (userId)
        savePendingRun({
          userId,
          sessionId: turn.sessionId,
          turnId: turn.turnId,
          clientRequestId: turn.clientRequestId,
          runId: null,
          lastSequence: 0,
          runMode: turn.runMode,
        })
      await streamRun(
        {
          sessionId: turn.sessionId,
          turnId: turn.turnId,
          clientRequestId: turn.clientRequestId,
          content: turn.content,
          ...(turn.modelId ? { modelId: turn.modelId } : {}),
          ...(turn.budgetPreset ? { budgetPreset: turn.budgetPreset } : {}),
          ...(turn.runMode ? { runMode: turn.runMode } : {}),
        },
        {
          signal: connection.signal,
          onRunId: (runId) => {
            if (!isCurrent()) return
            activeRunRef.current = { runId, lastSequence: 0 }
            if (userId) {
              const saved = readPendingRun(userId, turn.sessionId)
              if (saved?.clientRequestId === turn.clientRequestId)
                savePendingRun({ ...saved, runId })
            }
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
        lifecycle.terminal,
        activeRunRef.current !== null
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
          transcript = recoveryTranscript(detail.messages, turn)
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
      const userId = getAuthController().getSnapshot().user?.id
      if (userId) clearPendingRun(userId, turn.sessionId, turn.clientRequestId)
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
          logFailure(turnError, "history.load")
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
      if (isCurrent()) logFailure(turnError, "run.start")
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
      if (bootstrapControllerRef.current) logFailure(cancelError, "run.cancel")
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
        if (bootstrapControllerRef.current)
          logFailure(responseError, "run.respond")
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
      if (bootstrapControllerRef.current) logFailure(saveError, "settings.save")
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
      (route.kind === "chat" && activeSession?.id !== route.chatId) ||
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
    if (awaitingClarification && pendingClarification && activeRunId) {
      if (!content || clarificationSubmitting || cancellingRun) return
      const owner = {
        sessionId: activeSessionIdRef.current,
        generation: runGenerationRef.current,
      }
      let request = clarificationRequestRef.current
      if (
        request?.runId !== activeRunId ||
        request.clarificationId !== pendingClarification.id ||
        request.text !== content
      ) {
        request = {
          runId: activeRunId,
          clarificationId: pendingClarification.id,
          text: content,
          clientRequestId: crypto.randomUUID(),
        }
        clarificationRequestRef.current = request
      }
      setClarificationSubmitting(true)
      setError(null)
      try {
        const info = await clarifyResearch(request)
        if (
          !ownsRun(owner, {
            sessionId: activeSessionIdRef.current,
            generation: runGenerationRef.current,
          })
        )
          return
        if (activeRunTurnId) recordRunInfo(activeRunTurnId, info)
        setComposerValue("")
        clarificationRequestRef.current = null
        await researchQuery.refetch()
      } catch (clarificationError) {
        if (
          ownsRun(owner, {
            sessionId: activeSessionIdRef.current,
            generation: runGenerationRef.current,
          })
        )
          setError(visibleError(clarificationError))
      } finally {
        if (
          ownsRun(owner, {
            sessionId: activeSessionIdRef.current,
            generation: runGenerationRef.current,
          })
        )
          setClarificationSubmitting(false)
      }
      return
    }
    if (
      !content ||
      pending ||
      sessionLoading ||
      (route.kind === "chat" && activeSession?.id !== route.chatId) ||
      submissionLockRef.current ||
      modelSaveOwnerRef.current ||
      sessionModelSaving
    )
      return
    const model = settings?.models?.find((item) => item.id === selectedModelId)
    if (!isModelSelectable(model)) {
      setError("Choose a model from the list before starting a session.")
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
        const createdSession = await api
          .createSession()
          .catch((createError: unknown) => {
            if (bootstrapControllerRef.current && isCurrent())
              logFailure(createError, "sessions.create")
            throw createError
          })
        if (!isCurrent()) return
        session = createdSession
        sessionLoadSequenceRef.current += 1
        activeSessionIdRef.current = createdSession.id
        owner = { ...owner, sessionId: createdSession.id }
        setActiveSession(createdSession)
        cacheSession(queryClient, createdSession)
        setActiveView("session")
        appliedPathRef.current = chatPath(createdSession.id)
        navigate(chatPath(createdSession.id), true)
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
          clientRequestId: crypto.randomUUID(),
          content,
          modelId: model.id,
          budgetPreset: selectedBudgetPreset,
          runMode,
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
      !allowsRunRetry(
        runActivities[failedTurn.turnId],
        latestRun?.turn_id === failedTurn.turnId ? latestRun : null
      ) ||
      pending ||
      sessionLoading ||
      (route.kind === "chat" && activeSession?.id !== route.chatId) ||
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
      setError("Choose a model from the list before retrying.")
      return
    }
    submissionLockRef.current = true
    setPending(true)
    let preservePending = false
    const completion = completeTurn(
      {
        ...failedTurn,
        clientRequestId: crypto.randomUUID(),
        modelId: selectedModelId,
      },
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

  const acknowledgeWorkspaceReview = async (
    runId: string,
    toolCallId: string,
    sourceSessionId?: string
  ) => {
    const sessionId = activeSessionIdRef.current
    const generation = runGenerationRef.current
    const matchesWorkspace =
      workspaceReview?.run_id === runId &&
      workspaceReview.uncertain_operation?.tool_call_id === toolCallId &&
      workspaceReview.source_session_id === sourceSessionId
    const matchesLatest =
      latestRun?.run_id === runId &&
      latestRun.uncertain_operation?.tool_call_id === toolCallId &&
      (!sourceSessionId || sourceSessionId === sessionId)
    if (!sessionId || (!matchesWorkspace && !matchesLatest))
      throw new ApiError(
        "workspace_review_stale",
        "The workspace review changed. Reconnect and check the current operation.",
        409
      )
    const saved = await api.reviewWorkspace(
      sourceSessionId ?? sessionId,
      runId,
      toolCallId
    )
    if (
      activeSessionIdRef.current !== sessionId ||
      runGenerationRef.current !== generation
    )
      return
    if (saved.tool_call_id !== toolCallId)
      throw new RuntimeError(
        "runtime_protocol_error",
        "Trellis returned a different workspace review."
      )
    workspaceReviewRequestRef.current += 1
    setWorkspaceReview((current) =>
      current?.run_id === runId &&
      current.uncertain_operation?.tool_call_id === toolCallId
        ? {
            ...current,
            blocked: false,
            uncertain_operation: {
              ...current.uncertain_operation,
              reviewed_at: saved.reviewed_at,
              review_pending: false,
            },
          }
        : current
    )
    setLatestRun((current) =>
      current?.run_id === runId &&
      current.uncertain_operation?.tool_call_id === toolCallId
        ? {
            ...current,
            uncertain_operation: {
              ...current.uncertain_operation,
              reviewed_at: saved.reviewed_at,
              review_pending: false,
            },
          }
        : current
    )
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
      if (bootstrapControllerRef.current)
        logFailure(restoreError, "run.restore")
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
  const activeMode =
    activeActivity?.runInfo?.runMode ??
    activeActivity?.summary?.run_mode ??
    (latestRun?.run_id === activeRunId ? latestRun.run_mode : undefined)
  const researchQuery = useQuery({
    ...researchQueryOptions(activeSession?.id ?? "", activeRunId ?? ""),
    enabled: Boolean(activeSession && activeRunId && activeMode === "research"),
    refetchInterval: pending && activeMode === "research" ? 2500 : false,
  })
  const pendingClarification = researchQuery.data?.pending_clarification
  const awaitingClarification = Boolean(
    pending &&
    !cancellingRun &&
    activeActivity?.runInfo?.status === "waiting_for_input"
  )
  const recovering = pending && activeActivity?.runInfo?.status === "recovering"
  const activeBudgetPreset = activeActivity?.runInfo?.budgetPreset
  const lockedBudgetPreset =
    pending &&
    (activeBudgetPreset === "conservative" || activeBudgetPreset === "longer")
      ? activeBudgetPreset
      : selectedBudgetPreset
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
      (route.kind !== "chat" || activeSession?.id === route.chatId) &&
      !modelSaving &&
      runMode === "chat" &&
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
          activeSessionId={route.kind === "chat" ? route.chatId : null}
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
              onRetryActivityHistory={() => void loadSession(activeSession.id)}
              canRetry={
                failedTurn !== null &&
                !pending &&
                !modelSaving &&
                allowsRunRetry(
                  runActivities[failedTurn.turnId],
                  latestRun?.turn_id === failedTurn.turnId ? latestRun : null
                )
              }
              recovery={latestRun}
              workspaceReview={workspaceReview}
              onWorkspaceReview={acknowledgeWorkspaceReview}
              onRefreshWorkspaceReview={() =>
                void refreshWorkspaceReview(activeSession.id)
              }
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
          ) : route.kind === "chat" ? (
            <div className="welcome-state">
              {sessionLoading || !error ? (
                <p role="status">Loading chat…</p>
              ) : (
                <div className="chat-error welcome-error" role="alert">
                  <span>{error}</span>
                  <button
                    type="button"
                    onClick={() => void loadSession(route.chatId)}
                  >
                    Retry
                  </button>
                </div>
              )}
            </div>
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
              clarification={
                awaitingClarification ? (
                  <section
                    className="research-clarification"
                    aria-label="Research clarification"
                  >
                    <h2>One quick question</h2>
                    {pendingClarification ? (
                      <p>{pendingClarification.question}</p>
                    ) : (
                      <p role="status">
                        {researchQuery.error
                          ? "The saved question could not be loaded."
                          : "Loading the research question…"}
                        {researchQuery.error ? (
                          <button
                            type="button"
                            onClick={() => void researchQuery.refetch()}
                          >
                            Retry
                          </button>
                        ) : null}
                      </p>
                    )}
                    <p className="research-clarification-note" role="status">
                      {clarificationSubmitting
                        ? "Saving your reply…"
                        : "Research paused. Reply to continue."}
                    </p>
                  </section>
                ) : null
              }
              value={composerValue}
              placeholder={
                awaitingClarification
                  ? "Your reply"
                  : activeView === "New session"
                    ? "What are we building?"
                    : "Adjust or continue"
              }
              onChange={setComposerValue}
              onSubmit={() => void submitComposer()}
              disabled={
                (pending && !awaitingClarification) ||
                (awaitingClarification && !pendingClarification) ||
                clarificationSubmitting ||
                sessionLoading ||
                modelSaving ||
                (route.kind === "chat" && activeSession?.id !== route.chatId)
              }
              runStatus={
                sessionLoading
                  ? "Checking current run…"
                  : awaitingClarification
                    ? undefined
                    : pending
                      ? cancellingRun
                        ? "Stopping…"
                        : reconnectAvailable || error?.includes("Reconnecting")
                          ? "Reconnecting. This chat stays locked until its run is checked."
                          : recovering
                            ? "Recovering. Checking saved work before continuing."
                            : "Running. You can close this page and return to this chat."
                      : undefined
              }
              budgetPreset={lockedBudgetPreset}
              researchExecutionBudgetSeconds={
                pending && researchQuery.data?.run_id === activeRunId
                  ? researchQuery.data.execution_budget_seconds
                  : undefined
              }
              runMode={pending ? (activeMode ?? runMode) : runMode}
              onRunModeChange={setRunMode}
              researchAvailable={
                settings?.research_available === true &&
                settings.models?.find((model) => model.id === lockedModelId)
                  ?.supports_tools === true
              }
              researchUnavailableReason={
                settings?.research_available
                  ? "Choose a model that supports tools for research."
                  : settings?.research_unavailable_reason
              }
              answeringClarification={awaitingClarification}
              controlsDisabled={
                pending ||
                sessionLoading ||
                modelSaving ||
                (route.kind === "chat" && activeSession?.id !== route.chatId)
              }
              onBudgetChange={setRunBudgetChoice}
              models={settings?.models ?? []}
              selectedModelId={lockedModelId}
              modelSaving={modelSaving}
              onModelChange={(modelId) => void changeChatModel(modelId)}
              contextGauge={
                (pending ? (activeMode ?? runMode) : runMode) ===
                "research" ? undefined : (
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
                        : contextPreview.loading ||
                          sessionLoading ||
                          modelSaving
                    }
                    error={pending ? null : contextPreview.error}
                    compacting={compacting}
                  />
                )
              }
            />
          </>
        ) : null}
      </section>
    </main>
  )
}
