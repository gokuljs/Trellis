import { FolderClosed, X } from "lucide-react"
import { useState } from "react"

import { ApiError } from "@/lib/api"

type WorkspaceAttachmentProps = {
  workspacePath: string | null
  disabled: boolean
  onSave: (path: string) => Promise<void>
  onRemove: () => Promise<void>
}

export function WorkspaceAttachment({
  workspacePath,
  disabled,
  onSave,
  onRemove,
}: WorkspaceAttachmentProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(workspacePath ?? "")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const busy = disabled || saving

  const save = async () => {
    const path = draft.trim()
    if (!path.startsWith("/")) {
      setError("Enter an absolute folder path.")
      return
    }
    setSaving(true)
    setError(null)
    try {
      await onSave(path)
      setEditing(false)
    } catch (saveError) {
      setError(
        saveError instanceof ApiError
          ? saveError.message
          : "Trellis could not attach that folder."
      )
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    setSaving(true)
    setError(null)
    try {
      await onRemove()
      setDraft("")
      setEditing(false)
    } catch (removeError) {
      setError(
        removeError instanceof ApiError
          ? removeError.message
          : "Trellis could not remove that folder."
      )
    } finally {
      setSaving(false)
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
            setDraft(workspacePath ?? "")
            setError(null)
            setEditing((current) => !current)
          }}
          disabled={busy}
        >
          {workspacePath ? "Change workspace" : "Attach workspace"}
        </button>
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
          {error ? <div role="alert">{error}</div> : null}
        </form>
      ) : null}
    </div>
  )
}
