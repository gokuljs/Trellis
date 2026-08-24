import { Boxes, Plus } from "lucide-react"

import type { NavigationItem, Session } from "./app-types"

export const NAVIGATION_ITEMS: NavigationItem[] = [
  { label: "New session", icon: Plus },
  { label: "Capabilities", icon: Boxes },
]

export const INITIAL_SESSIONS: Session[] = []
