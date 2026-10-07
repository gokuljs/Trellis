import { useState } from "react"

import { ApiError, api } from "@/lib/api"
import type { TestPreset } from "@/lib/app-types"

type TestPresetsProps = {
  sessionId: string
  disabled: boolean
}

export function TestPresets({ sessionId, disabled }: TestPresetsProps) {
  const [open, setOpen] = useState(false)
  const [presets, setPresets] = useState<TestPreset[]>([])
  const [name, setName] = useState("")
  const [command, setCommand] = useState("")
  const [cwd, setCwd] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = async () => {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    setBusy(true)
    setError(null)
    try {
      setPresets(await api.listTestPresets(sessionId))
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "Could not load test commands."
      )
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const saved = await api.saveTestPreset(sessionId, {
        name: name.trim(),
        command: command.trim(),
        cwd: cwd.trim() || ".",
      })
      setPresets((current) =>
        [...current.filter((item) => item.name !== saved.name), saved].sort(
          (a, b) => a.name.localeCompare(b.name)
        )
      )
      setName("")
      setCommand("")
      setCwd("")
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "Could not save the test command."
      )
    } finally {
      setBusy(false)
    }
  }

  const remove = async (preset: TestPreset) => {
    setBusy(true)
    setError(null)
    try {
      await api.deleteTestPreset(sessionId, preset.name)
      setPresets((current) =>
        current.filter((item) => item.name !== preset.name)
      )
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "Could not remove the test command."
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="test-presets">
      <button type="button" onClick={() => void toggle()} disabled={disabled}>
        Test commands
      </button>
      {open ? (
        <div className="test-presets-panel">
          <p>
            Saved test commands run automatically without approval, using the
            local Trellis process&apos;s filesystem permissions.
          </p>
          {presets.length > 0 ? (
            <ul>
              {presets.map((preset) => (
                <li key={preset.name}>
                  <span>
                    <strong>{preset.name}</strong> <code>{preset.command}</code>
                    {preset.cwd !== "." ? <small>in {preset.cwd}</small> : null}
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${preset.name}`}
                    disabled={disabled || busy}
                    onClick={() => void remove(preset)}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void save()
            }}
          >
            <label htmlFor="test-preset-name">Name</label>
            <input
              id="test-preset-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={64}
              required
              disabled={disabled || busy}
            />
            <label htmlFor="test-preset-command">Command</label>
            <input
              id="test-preset-command"
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              maxLength={2048}
              required
              placeholder="python -m pytest"
              disabled={disabled || busy}
            />
            <label htmlFor="test-preset-cwd">Working folder</label>
            <input
              id="test-preset-cwd"
              value={cwd}
              onChange={(event) => setCwd(event.target.value)}
              maxLength={4096}
              placeholder="."
              disabled={disabled || busy}
            />
            <button type="submit" disabled={disabled || busy}>
              Save test command
            </button>
          </form>
          {error ? <div role="alert">{error}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
