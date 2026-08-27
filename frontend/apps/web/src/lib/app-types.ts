import type { LucideIcon } from "lucide-react"

export type WorkspaceView =
  "New session" | "Capabilities" | "Settings" | "session"
export type ProviderId = "openai" | "anthropic"
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

export type Settings = {
  selected_provider: ProviderId
  providers: ProviderStatus[]
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

export type TurnResult = {
  session: Session
  user_message: Message
  assistant_message: Message
}
