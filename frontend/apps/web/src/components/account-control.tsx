import { ChevronDown, LogOut, UserRound } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { useAuth } from "@/lib/use-auth"
import "./account-control.css"

export function AccountControl() {
  const auth = useAuth()
  const menuRef = useRef<HTMLDetailsElement>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      const menu = menuRef.current
      if (
        menu &&
        event.target instanceof Node &&
        !menu.contains(event.target)
      ) {
        menu.open = false
      }
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      const menu = menuRef.current
      if (event.key === "Escape" && menu?.open) {
        menu.open = false
        menu.querySelector("summary")?.focus()
      }
    }
    document.addEventListener("pointerdown", closeOutside)
    document.addEventListener("keydown", closeOnEscape)
    return () => {
      document.removeEventListener("pointerdown", closeOutside)
      document.removeEventListener("keydown", closeOnEscape)
    }
  }, [])

  if (!auth.user) return null

  const metadata = auth.user.user_metadata
  const name = [
    metadata?.full_name,
    metadata?.name,
    metadata?.user_name,
    auth.user.email,
  ].find(
    (value): value is string =>
      typeof value === "string" && value.trim().length > 0
  )
  const provider = auth.user.app_metadata?.provider
  const displayName =
    name?.trim().slice(0, 100) ??
    (provider === "github"
      ? "GitHub account"
      : provider === "google"
        ? "Google account"
        : "Trellis account")

  const handleSignOut = async () => {
    if (busy) return
    setBusy(true)
    try {
      await auth.signOut()
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="account-menu" ref={menuRef}>
      <summary
        className="account-trigger"
        aria-label={`Account: ${displayName}`}
        title={displayName}
      >
        <UserRound size={16} aria-hidden="true" />
        <span className="account-label">{displayName}</span>
        <ChevronDown className="account-chevron" size={12} aria-hidden="true" />
      </summary>
      <div className="account-panel">
        <p className="account-name">{displayName}</p>
        {auth.user.email ? (
          <p className="account-email">{auth.user.email}</p>
        ) : null}
        {auth.error ? (
          <p className="account-error" role="alert">
            {auth.error}
          </p>
        ) : null}
        <button
          className="account-sign-out"
          type="button"
          disabled={busy}
          onClick={() => {
            void handleSignOut()
          }}
        >
          <LogOut size={16} aria-hidden="true" />
          {busy ? "Signing out…" : "Sign out"}
        </button>
        {busy ? (
          <p className="sr-only" role="status">
            Signing out…
          </p>
        ) : null}
      </div>
    </details>
  )
}
