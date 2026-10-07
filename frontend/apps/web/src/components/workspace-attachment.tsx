import { FolderClosed, Plus, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { ApiError } from "@/lib/api"

type WorkspaceAttachmentProps = {
  workspacePath: string | null
  disabled: boolean
  onPickWorkspace: () => Promise<boolean>
  onSave: (path: string) => Promise<void>
  onRemove: () => Promise<void>
}

const workspaceMenuId = "composer-workspace-menu"

function workspaceName(path: string) {
  const withoutTrailingSeparators = path.replace(/[\\/]+$/, "")
  return withoutTrailingSeparators.split(/[\\/]/).at(-1) || path
}

export function WorkspaceAttachment({
  workspacePath,
  disabled,
  onPickWorkspace,
  onSave,
  onRemove,
}: WorkspaceAttachmentProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(workspacePath ?? "")
  const [busyAction, setBusyAction] = useState<
    "pick" | "save" | "remove" | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [manualEntryAvailable, setManualEntryAvailable] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const workspaceItemRef = useRef<HTMLButtonElement>(null)
  const manualInputRef = useRef<HTMLInputElement>(null)
  const restoreFocusRef = useRef(false)
  const busy = disabled || busyAction !== null

  const closeMenuAndRestoreFocus = useCallback(() => {
    restoreFocusRef.current = true
    setMenuOpen(false)
  }, [])

  useEffect(() => {
    if (!menuOpen) return

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (
        busyAction === null &&
        !anchorRef.current?.contains(event.target as Node)
      ) {
        setMenuOpen(false)
      }
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && busyAction === null) {
        closeMenuAndRestoreFocus()
      }
    }

    document.addEventListener("pointerdown", closeOnOutsidePointer)
    document.addEventListener("keydown", closeOnEscape)
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer)
      document.removeEventListener("keydown", closeOnEscape)
    }
  }, [busyAction, closeMenuAndRestoreFocus, menuOpen])

  useEffect(() => {
    if (!menuOpen && !busy && restoreFocusRef.current) {
      triggerRef.current?.focus()
      restoreFocusRef.current = false
    }
  }, [busy, menuOpen])

  useEffect(() => {
    if (!menuOpen) return
    if (editing) {
      manualInputRef.current?.focus()
    } else {
      workspaceItemRef.current?.focus()
    }
  }, [editing, menuOpen])

  const save = async () => {
    const path = draft.trim()
    if (!path.startsWith("/")) {
      setError("Enter an absolute folder path.")
      return
    }
    setBusyAction("save")
    setError(null)
    try {
      await onSave(path)
      setEditing(false)
      setManualEntryAvailable(false)
      closeMenuAndRestoreFocus()
    } catch (saveError) {
      setError(
        saveError instanceof ApiError
          ? saveError.message
          : "Trellis could not attach that folder."
      )
    } finally {
      setBusyAction(null)
    }
  }

  const pickWorkspace = async () => {
    setBusyAction("pick")
    setError(null)
    try {
      const saved = await onPickWorkspace()
      if (saved) {
        setEditing(false)
        setManualEntryAvailable(false)
        closeMenuAndRestoreFocus()
      } else if (!editing) {
        closeMenuAndRestoreFocus()
      }
    } catch (pickerError) {
      setError(
        pickerError instanceof ApiError
          ? pickerError.message
          : "Trellis could not open the folder picker."
      )
      setManualEntryAvailable(
        pickerError instanceof ApiError &&
          pickerError.code === "workspace_picker_unavailable"
      )
      setMenuOpen(true)
    } finally {
      setBusyAction(null)
    }
  }

  const remove = async () => {
    setBusyAction("remove")
    setError(null)
    try {
      await onRemove()
      setDraft("")
      setEditing(false)
      setManualEntryAvailable(false)
      closeMenuAndRestoreFocus()
    } catch (removeError) {
      setError(
        removeError instanceof ApiError
          ? removeError.message
          : "Trellis could not remove that folder."
      )
      setMenuOpen(true)
    } finally {
      setBusyAction(null)
    }
  }

  return (
    <>
      {workspacePath ? (
        <div className="composer-workspace-chip">
          <span>{workspaceName(workspacePath)}</span>
          <button
            type="button"
            aria-label="Remove workspace"
            onClick={() => void remove()}
            disabled={busy}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </div>
      ) : null}
      <div className="composer-workspace-anchor" ref={anchorRef}>
        <button
          className="composer-add"
          type="button"
          aria-label="Attach"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={workspaceMenuId}
          onClick={() => {
            if (menuOpen) {
              setMenuOpen(false)
            } else {
              setError(null)
              setMenuOpen(true)
            }
          }}
          disabled={busy}
          ref={triggerRef}
        >
          <Plus size={18} strokeWidth={1.6} aria-hidden="true" />
        </button>
        {menuOpen ? (
          <div
            id={workspaceMenuId}
            className="composer-workspace-menu"
            role={editing ? "dialog" : "menu"}
            aria-label={editing ? "Enter workspace path" : "Add to chat"}
            aria-busy={busyAction !== null}
            onKeyDown={(event) => {
              if (editing) return
              if (event.key === "Tab" && busyAction === null) {
                event.preventDefault()
                setMenuOpen(false)
                anchorRef.current?.parentElement
                  ?.querySelector<HTMLTextAreaElement>(
                    'textarea[aria-label="Message"]'
                  )
                  ?.focus()
                return
              }
              const items = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  '[role="menuitem"]:not(:disabled)'
                )
              )
              if (items.length === 0) return

              const currentIndex = items.indexOf(
                document.activeElement as HTMLButtonElement
              )
              let nextIndex: number | null = null
              if (event.key === "ArrowDown") {
                nextIndex = (currentIndex + 1) % items.length
              } else if (event.key === "ArrowUp") {
                nextIndex = (currentIndex - 1 + items.length) % items.length
              } else if (event.key === "Home") {
                nextIndex = 0
              } else if (event.key === "End") {
                nextIndex = items.length - 1
              }

              if (nextIndex !== null) {
                event.preventDefault()
                items[nextIndex]?.focus()
              }
            }}
          >
            {editing ? (
              <form
                className="composer-workspace-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  void save()
                }}
              >
                <label htmlFor="workspace-folder">Workspace folder</label>
                <input
                  id="workspace-folder"
                  ref={manualInputRef}
                  type="text"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder="/absolute/path/to/project"
                  autoCapitalize="off"
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={4096}
                  disabled={busy}
                />
                <div className="composer-workspace-form-actions">
                  <button type="submit" disabled={busy}>
                    {busyAction === "save" ? "Saving…" : "Save workspace"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(false)
                      setError(null)
                    }}
                    disabled={busy}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() => void pickWorkspace()}
                    disabled={busy}
                  >
                    Choose folder…
                  </button>
                </div>
              </form>
            ) : (
              <>
                <button
                  className="composer-workspace-menu-item"
                  type="button"
                  role="menuitem"
                  onClick={() => void pickWorkspace()}
                  disabled={busy}
                  ref={workspaceItemRef}
                >
                  <FolderClosed size={16} aria-hidden="true" />
                  <span>Workspace</span>
                </button>
                {manualEntryAvailable ? (
                  <button
                    className="composer-workspace-menu-item"
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setDraft(workspacePath ?? "")
                      setEditing(true)
                      setManualEntryAvailable(false)
                      setError(null)
                    }}
                    disabled={busy}
                  >
                    <span>Enter path manually</span>
                  </button>
                ) : null}
              </>
            )}
            {busyAction === "pick" ? (
              <div className="composer-workspace-status" role="status">
                Opening folder…
              </div>
            ) : null}
            {busyAction === "save" ? (
              <div className="composer-workspace-status" role="status">
                Saving workspace…
              </div>
            ) : null}
            {error ? <div role="alert">{error}</div> : null}
          </div>
        ) : null}
      </div>
    </>
  )
}
