import type { LucideIcon } from "lucide-react"

export type WorkspaceView = "New session" | "Settings" | "session"
export type ProviderId = string
export type ModelId = string
export type OnboardingStep = "intro" | "profile" | "model"

export type OnboardingState = {
  current_step: OnboardingStep | "complete"
  completed: boolean
}
export type MessageRole = "user" | "assistant"

export type NavigationItem = {
  label: Exclude<WorkspaceView, "session" | "Settings">
  icon: LucideIcon
}

export type Session = {
  id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  model_id?: ModelId | null
}

export type Profile = {
  id: string
  display_name: string | null
  email: string | null
  created_at: string
  updated_at: string
}

export type ProviderStatus = {
  id: ProviderId
  name: string
  model: string
  configured: boolean
  key_hint: string | null
}

export type ModelStatus = {
  id: ModelId
  provider_id: ProviderId
  provider_name: string
  adapter_kind: string
  upstream_model_id: string
  name: string
  requires_api_key: boolean
  supports_streaming: boolean
  supports_tools: boolean
  configured: boolean
  key_hint: string | null
  enabled?: boolean
  selectable?: boolean
  context_window_tokens?: number | null
  max_input_tokens?: number | null
  max_output_tokens?: number | null
  supports_native_compaction?: boolean | null
  metadata_status?: "ready" | "pending"
  pending_reasons?: string[]
  verification_sources?: string[]
  metadata_verified_at?: string | null
  availability?: "available" | "unavailable" | "unknown"
  availability_checked_at?: string | null
}

export type Settings = {
  selected_provider: ProviderId
  providers: ProviderStatus[]
  selected_model_id?: ModelId
  models?: ModelStatus[]
  default_budget_preset?: "conservative" | "longer"
  capabilities?: ("local_read" | "local_patch" | "local_command")[]
}

export type Message = {
  id: string
  turn_id: string
  role: MessageRole
  content: string
  provider: ProviderId | null
  model: string | null
  created_at: string
}

export type SessionDetail = {
  session: Session
  messages: Message[]
  assistant_outputs?: AssistantOutput[]
}

export type TimelineRow = {
  kind: "message" | "assistant_output"
  id: string
}

export type SessionHistoryPage = SessionDetail & {
  timeline_order: TimelineRow[]
  next_before_cursor: string | null
}

export type SessionPage = {
  items: Session[]
  next_before_cursor: string | null
}

export type AssistantOutput = {
  id: string
  turn_id: string
  run_id: string
  model_call_id: string
  step_index: number
  content: string
  status: "streaming" | "complete" | "partial"
  stop_reason: string | null
  promoted_message_id: string | null
  created_at: string
  next_batch_index?: number
}

export type LatestRun = {
  run_id: string
  turn_id: string
  status: string
  last_sequence: number
  input_message?: Message | null
}

export type RunSummary = {
  run_id: string
  turn_id: string
  retry_of: string | null
  status: string
  budget_preset: string
  max_model_calls: number
  max_tool_calls: number
  max_total_tokens: number
  max_cost_usd: number
  deadline_at: string | null
  last_sequence: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  model_id?: ModelId
}

export type ContextPreviewRequest = {
  session_id?: string
  model_id: ModelId
  draft: string
  budget_preset: "conservative" | "longer"
}

export type ContextPreview = {
  model_id: ModelId
  context_window_tokens: number | null
  input_tokens: number | null
  count_method: "provider" | "utf8_bytes_estimate_v1"
  output_reserve_tokens: number | null
  safety_margin_tokens: number | null
  usable_input_tokens: number | null
  compaction_threshold_tokens: number | null
  percentage: number | null
  status: "ready" | "compaction_required" | "compacting" | "unavailable"
  checkpoint_id?: string | null
}

export type SavedRunEvent = {
  run_id: string
  sequence: number
  event_type: string
  event_version: number
  data: Record<string, unknown>
  created_at: string
}
