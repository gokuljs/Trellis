import { FolderClosed, X } from "lucide-react"
import { useState } from "react"

import { ApiError } from "@/lib/api"

type WorkspaceAttachmentProps = {
  workspacePath: string | null
  disabled: boolean
  onPickWorkspace: () => Promise<boolean>
  onSave: (path: string) => Promise<void>
  onRemove: () => Promise<void>
}

export function WorkspaceAttachment({
  workspacePath,
  disabled,
  onPickWorkspace,
  onSave,
  onRemove,
}: WorkspaceAttachmentProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(workspacePath ?? "")
  const [busyAction, setBusyAction] = useState<
    "pick" | "save" | "remove" | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [manualEntryAvailable, setManualEntryAvailable] = useState(false)
  const busy = disabled || busyAction !== null

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
    setManualEntryAvailable(false)
    try {
      const saved = await onPickWorkspace()
      if (saved) setEditing(false)
    } catch (pickerError) {
      setError(
        pickerError instanceof ApiError
          ? pickerError.message
          : "Trellis could not open the folder picker."
      )
      setManualEntryAvailable(true)
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
    } catch (removeError) {
      setError(
        removeError instanceof ApiError
          ? removeError.message
          : "Trellis could not remove that folder."
      )
    } finally {
      setBusyAction(null)
    }
  }

  return (
    <div className="workspace-attachment">
      <div className="workspace-attachment-summary">
        <FolderClosed size={15} aria-hidden="true" />
        {workspacePath ? (
          <span className="workspace-attachment-path" title={workspacePath}>
            {workspacePath}
          </span>
        ) : (
          <span>Chat without a folder</span>
        )}
        <button
          type="button"
          onClick={() => {
            void pickWorkspace()
          }}
          disabled={busy}
        >
          {busyAction === "pick"
            ? "Opening folder…"
            : workspacePath
              ? "Change workspace"
              : "Attach workspace"}
        </button>
        {workspacePath ? (
          <button
            type="button"
            className="workspace-attachment-remove"
            onClick={() => void remove()}
            disabled={busy}
          >
            Remove workspace
          </button>
        ) : null}
      </div>
      {editing ? (
        <form
          className="workspace-attachment-editor"
          onSubmit={(event) => {
            event.preventDefault()
            void save()
          }}
        >
          <label htmlFor="workspace-folder">Workspace folder</label>
          <div className="workspace-attachment-input-row">
            <input
              id="workspace-folder"
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
            <button type="submit" disabled={busy}>
              Save workspace
            </button>
            <button
              type="button"
              className="workspace-attachment-close"
              aria-label="Close workspace editor"
              onClick={() => setEditing(false)}
              disabled={busy}
            >
              <X size={15} aria-hidden="true" />
            </button>
          </div>
        </form>
      ) : null}
      {error ? <div role="alert">{error}</div> : null}
      {manualEntryAvailable ? (
        <button
          type="button"
          className="workspace-attachment-manual"
          onClick={() => {
            setDraft((currentDraft) =>
              editing ? currentDraft : (workspacePath ?? "")
            )
            setEditing(true)
            setManualEntryAvailable(false)
          }}
          disabled={busy}
        >
          Enter path manually
        </button>
      ) : null}
    </div>
  )
}
