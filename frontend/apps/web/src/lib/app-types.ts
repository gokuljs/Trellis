import type { LucideIcon } from "lucide-react"

export type WorkspaceView = "New session" | "Capabilities" | "Settings" | "session"

export type NavigationItem = {
  label: Exclude<WorkspaceView, "session" | "Settings">
  icon: LucideIcon
}

export type Session = {
  title: string
  date: string
  active?: boolean
}
