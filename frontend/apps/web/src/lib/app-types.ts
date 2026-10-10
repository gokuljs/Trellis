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
  workspace_path: string | null
  workspace_ready: boolean
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
}

export type Settings = {
  selected_provider: ProviderId
  providers: ProviderStatus[]
  selected_model_id?: ModelId
  models?: ModelStatus[]
  default_budget_preset?: "conservative" | "longer"
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
}

export type LatestRun = {
  run_id: string
  turn_id: string
  status: string
  last_sequence: number
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
  deadline_at: string
  last_sequence: number
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type SavedRunEvent = {
  run_id: string
  sequence: number
  event_type: string
  event_version: number
  data: Record<string, unknown>
  created_at: string
}

export type TurnResult = {
  session: Session
  user_message: Message
  assistant_message: Message
}
