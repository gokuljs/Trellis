import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"

import { TestPresets } from "@/components/test-presets"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("saves an exact workspace test command and lets the user remove it", async () => {
  const presets: { name: string; command: string; cwd: string }[] = []
  const requests: { method: string; url: string; body?: unknown }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? "GET"
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      requests.push({ method, url, body })
      if (method === "POST") {
        presets.push(body)
        return new Response(JSON.stringify(body), { status: 201 })
      }
      if (method === "DELETE") {
        presets.splice(0, 1)
        return new Response(null, { status: 204 })
      }
      return new Response(JSON.stringify(presets), { status: 200 })
    })
  )

  const user = userEvent.setup()
  render(<TestPresets sessionId="session-1" disabled={false} />)
  await user.click(screen.getByRole("button", { name: "Test commands" }))
  await user.type(screen.getByLabelText("Name"), "Backend")
  await user.type(screen.getByLabelText("Command"), "python -m pytest")
  await user.type(screen.getByLabelText("Working folder"), "backend")
  await user.click(screen.getByRole("button", { name: "Save test command" }))

  expect(await screen.findByText("python -m pytest")).toBeInTheDocument()
  expect(
    screen.getByText(/local Trellis process's filesystem permissions/i)
  ).toBeInTheDocument()
  expect(requests).toContainEqual({
    method: "POST",
    url: "/api/sessions/session-1/test-presets",
    body: { name: "Backend", command: "python -m pytest", cwd: "backend" },
  })

  await user.click(screen.getByRole("button", { name: "Remove Backend" }))
  await waitFor(() => expect(screen.queryByText("python -m pytest")).toBeNull())
})
