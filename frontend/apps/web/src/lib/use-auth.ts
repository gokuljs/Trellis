import { useState, useSyncExternalStore } from "react"

import { getAuthController } from "@/lib/auth-controller"

export function useAuth() {
  const [controller] = useState(getAuthController)
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot
  )

  return {
    ...state,
    signIn: controller.signIn,
    signOut: controller.signOut,
    retry: controller.retry,
  }
}
