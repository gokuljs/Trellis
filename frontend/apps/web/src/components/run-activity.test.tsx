import { cleanup, render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { RunActivity } from "@/components/run-activity"
import type { RuntimeRunEvent, RuntimeRunInfo } from "@/lib/runtime-client"

const runInfo: RuntimeRunInfo = {
  runId: "run-1",
  budgetPreset: "conservative",
  limits: {
    maxModelCalls: 8,
    maxToolCalls: 16,
    maxTotalTokens: 100_000,
    maxCostUsd: 2,
    deadlineAt: "2026-10-07T12:10:00Z",
  },
}

function event(
  sequence: number,
  eventType: string,
  data: Record<string, unknown>
): RuntimeRunEvent {
  return { runId: "run-1", sequence, eventType, eventVersion: 1, data }
}

afterEach(cleanup)

describe("run activity", () => {
  it("shows the server timestamp beside readable activity", () => {
    const { container } = render(
      <RunActivity
        events={[
          {
            ...event(1, "run.started", { status: "running" }),
            createdAt: "2026-10-07T12:00:01Z",
          },
        ]}
        runInfo={null}
        pending
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    const timestamp = container.querySelector("[data-run-sequence='1'] time")
    expect(timestamp).toHaveAttribute("dateTime", "2026-10-07T12:00:01Z")
    expect(timestamp).toHaveAttribute("title", "2026-10-07T12:00:01Z")
    expect(timestamp).not.toHaveTextContent("Time unavailable")
  })

  it("exposes every event in sequence order and mounts payloads only when expanded", async () => {
    const user = userEvent.setup()
    const { container } = render(
      <RunActivity
        events={[
          {
            ...event(3, "future.event", {
              nested: { value: "<script>hello</script>" },
            }),
            eventVersion: 2,
            createdAt: "2026-10-07T12:00:03Z",
          },
          {
            ...event(1, "assistant.delta", { text: "Safe answer" }),
            createdAt: "2026-10-07T12:00:01Z",
          },
          {
            ...event(2, "assistant.completed", { message_id: "message-1" }),
            createdAt: "2026-10-07T12:00:02Z",
          },
        ]}
        runInfo={null}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    const logToggle = screen.getByText("Event log (3)")
    expect(
      screen.queryByRole("list", { name: "Complete run event log" })
    ).not.toBeInTheDocument()
    await user.click(logToggle)
    const log = screen.getByRole("list", { name: "Complete run event log" })
    expect(
      Array.from(log.querySelectorAll("[data-log-sequence]")).map((entry) =>
        entry.getAttribute("data-log-sequence")
      )
    ).toEqual(["1", "2", "3"])
    expect(within(log).getByText("assistant.delta")).toBeInTheDocument()
    expect(within(log).getByText("assistant.completed")).toBeInTheDocument()
    expect(
      within(log).queryByLabelText("Event 3 details")
    ).not.toBeInTheDocument()

    await user.click(within(log).getByLabelText("Event 3: future.event"))
    const payload = within(log).getByLabelText("Event 3 details")
    expect(JSON.parse(payload.textContent ?? "")).toEqual({
      runId: "run-1",
      sequence: 3,
      eventType: "future.event",
      eventVersion: 2,
      createdAt: "2026-10-07T12:00:03Z",
      data: { nested: { value: "<script>hello</script>" } },
    })
    expect(container.querySelector("script")).toBeNull()
    expect(
      within(log).getByLabelText("Event 3: future.event").querySelector("time")
    ).toHaveAttribute("dateTime", "2026-10-07T12:00:03Z")
    await user.click(within(log).getByLabelText("Event 3: future.event"))
    expect(
      within(log).queryByLabelText("Event 3 details")
    ).not.toBeInTheDocument()
    await user.click(logToggle)
    expect(
      screen.queryByRole("list", { name: "Complete run event log" })
    ).not.toBeInTheDocument()
  })

  it("keeps missing and invalid timestamps inspectable without inventing a time", async () => {
    const user = userEvent.setup()
    render(
      <RunActivity
        events={[
          event(1, "assistant.delta", { text: "Answer" }),
          {
            ...event(2, "assistant.completed", { message_id: "message-1" }),
            createdAt: "invalid",
          },
        ]}
        runInfo={null}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    await user.click(screen.getByText("Event log (2)"))
    const log = screen.getByRole("list", { name: "Complete run event log" })
    expect(within(log).getAllByText("Time unavailable")).toHaveLength(2)
    expect(log.querySelector("time")).toBeNull()
    await user.click(within(log).getByLabelText("Event 2: assistant.completed"))
    expect(
      JSON.parse(
        within(log).getByLabelText("Event 2 details").textContent ?? ""
      ).createdAt
    ).toBe("invalid")
  })

  it("adds incoming events to an open log and keeps an expanded payload open", async () => {
    const user = userEvent.setup()
    const queued = {
      ...event(1, "run.queued", { status: "queued" }),
      createdAt: "2026-10-07T12:00:00Z",
    }
    const props = {
      runInfo: null,
      pending: true,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: vi.fn(),
    }
    const { rerender } = render(<RunActivity {...props} events={[queued]} />)

    await user.click(screen.getByText("Event log (1)"))
    await user.click(screen.getByLabelText("Event 1: run.queued"))
    rerender(
      <RunActivity
        {...props}
        events={[
          queued,
          {
            ...event(2, "run.started", { status: "running" }),
            createdAt: "2026-10-07T12:00:01Z",
          },
        ]}
      />
    )

    expect(screen.getByText("Event log (2)")).toBeInTheDocument()
    expect(screen.getByLabelText("Event 2: run.started")).toBeInTheDocument()
    expect(screen.getByLabelText("Event 1 details")).toBeInTheDocument()
  })

  it("keeps model, tool, and result activity in saved event order with budget usage", () => {
    const { container } = render(
      <RunActivity
        events={[
          event(1, "run.queued", { status: "queued" }),
          event(2, "run.started", { status: "running" }),
          event(3, "model.usage", {
            input_tokens: 120,
            output_tokens: 30,
            total_tokens: 150,
            total_estimated_cost_usd: 0.0042,
          }),
          event(4, "model.completed", { finish_reason: "tool_use" }),
          event(5, "tool.call", {
            tool_call_id: "tool-1",
            name: "read_file",
            arguments: { path: "src/main.ts" },
          }),
          event(6, "tool.result", {
            tool_call_id: "tool-1",
            status: "completed",
            content: "export const ready = true",
          }),
          event(7, "run.completed", { status: "completed" }),
        ]}
        runInfo={runInfo}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    expect(screen.getByText("Standard budget")).toBeInTheDocument()
    expect(screen.getByText("1 / 8 model calls")).toBeInTheDocument()
    expect(screen.getByText("1 / 16 tools")).toBeInTheDocument()
    expect(screen.getByText("150 / 100,000 tokens")).toBeInTheDocument()
    expect(screen.getByText("$0.0042 / $2.00 estimated")).toBeInTheDocument()
    expect(screen.getByText(/Deadline/).closest("time")).toHaveAttribute(
      "dateTime",
      "2026-10-07T12:10:00Z"
    )
    expect(
      Array.from(container.querySelectorAll("[data-run-sequence]")).map(
        (item) => Number(item.getAttribute("data-run-sequence"))
      )
    ).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(screen.getByText("src/main.ts")).toBeInTheDocument()
    expect(screen.getByText("export const ready = true")).toBeInTheDocument()
    expect(screen.getByText("Run completed")).toBeInTheDocument()
  })

  it("shows the extended run budget label", () => {
    render(
      <RunActivity
        events={[event(1, "run.started", { status: "running" })]}
        runInfo={{ ...runInfo, budgetPreset: "longer" }}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    expect(screen.getByText("Extended budget")).toBeInTheDocument()
  })

  it("shows a reviewable preview and sends only the selected approval decision", async () => {
    const onApprovalDecision = vi.fn()
    render(
      <RunActivity
        events={[
          event(1, "tool.call", {
            tool_call_id: "tool-edit",
            name: "apply_patch",
            arguments: { path: "src/main.ts" },
          }),
          event(2, "tool.approval_requested", {
            tool_call_id: "tool-edit",
            name: "apply_patch",
            preview: {
              path: "src/main.ts",
              diff: "-const old = true\n+const old = false",
            },
          }),
        ]}
        runInfo={runInfo}
        pending
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={onApprovalDecision}
      />
    )

    const approval = screen.getByRole("region", {
      name: "Approval required for apply_patch",
    })
    expect(within(approval).getByText("src/main.ts")).toBeInTheDocument()
    expect(within(approval).getByText(/const old = false/)).toBeInTheDocument()
    await userEvent.click(
      within(approval).getByRole("button", { name: "Approve" })
    )
    expect(onApprovalDecision).toHaveBeenCalledWith("tool-edit", "approved")
  })

  it("shows denial and failure without leaving stale approval controls", () => {
    render(
      <RunActivity
        events={[
          event(1, "tool.approval_requested", {
            tool_call_id: "tool-edit",
            name: "apply_patch",
            preview: { path: "src/main.ts" },
          }),
          event(2, "tool.approval_decided", {
            tool_call_id: "tool-edit",
            decision: "denied",
          }),
          event(3, "tool.result", {
            tool_call_id: "tool-edit",
            status: "denied",
            content: "approval_denied: The user denied this tool call.",
          }),
          event(4, "run.failed", {
            code: "cost_limit",
            message: "The run reached its budget limit.",
          }),
        ]}
        runInfo={runInfo}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    expect(screen.getByText("Denied")).toBeInTheDocument()
    expect(screen.getByText("Cost limit reached")).toBeInTheDocument()
    expect(
      screen.getByText("The run reached its budget limit.")
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Approve" })
    ).not.toBeInTheDocument()
  })

  it("disables decisions while responding and clears them when cancelled", () => {
    const approval = event(1, "tool.approval_requested", {
      tool_call_id: "tool-command",
      name: "run_command",
      preview: { command: "git status", cwd: "." },
    })
    const onApprovalDecision = vi.fn()
    const { rerender } = render(
      <RunActivity
        events={[approval]}
        runInfo={runInfo}
        pending
        approvalPendingToolId="tool-command"
        approvalError={null}
        onApprovalDecision={onApprovalDecision}
      />
    )

    expect(screen.getByText("git status")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Deny" })).toBeDisabled()

    rerender(
      <RunActivity
        events={[approval]}
        runInfo={runInfo}
        pending
        approvalPendingToolId={null}
        approvalError="This approval could not be sent."
        onApprovalDecision={onApprovalDecision}
      />
    )

    expect(screen.getByRole("alert")).toHaveTextContent(
      "This approval could not be sent."
    )
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled()

    rerender(
      <RunActivity
        events={[
          approval,
          event(2, "run.cancelled", { message: "The run was cancelled." }),
        ]}
        runInfo={runInfo}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={onApprovalDecision}
      />
    )

    expect(screen.getByText("Run cancelled")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Approve" })
    ).not.toBeInTheDocument()
  })

  it("shows the full request and closes approval when cancellation begins", () => {
    const request = event(1, "tool.approval_requested", {
      tool_call_id: "tool-search",
      name: "search_files",
      arguments: { path: "src", query: "needle", case_sensitive: false },
      preview: null,
    })
    const cancellation = event(2, "run.cancellation_requested", {
      status: "cancelling",
    })
    const { rerender } = render(
      <RunActivity
        events={[request, cancellation]}
        runInfo={runInfo}
        pending
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    const approval = screen.getByRole("region", {
      name: "Approval required for search_files",
    })
    expect(within(approval).getByText(/"query": "needle"/)).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Approve" })
    ).not.toBeInTheDocument()

    rerender(
      <RunActivity
        events={[
          request,
          cancellation,
          event(3, "run.failed", {
            code: "time_limit",
            message: "The run reached its budget limit.",
          }),
        ]}
        runInfo={runInfo}
        pending={false}
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )
    expect(screen.getByText("Time limit reached")).toBeInTheDocument()
  })

  it("updates the displayed deadline from a persisted resume event", () => {
    const events = [
      event(1, "tool.approval_requested", {
        tool_call_id: "tool-edit",
        name: "apply_patch",
      }),
      event(2, "tool.approval_decided", {
        tool_call_id: "tool-edit",
        decision: "approved",
      }),
      event(3, "run.resumed", {
        tool_call_id: "tool-edit",
        deadline_at: "2026-10-07T12:30:00Z",
      }),
    ]
    const { rerender } = render(
      <RunActivity
        events={events}
        runInfo={runInfo}
        pending
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )

    expect(screen.getByText(/Deadline/).closest("time")).toHaveAttribute(
      "dateTime",
      "2026-10-07T12:30:00Z"
    )

    rerender(
      <RunActivity
        events={[
          ...events,
          event(4, "run.resumed", {
            tool_call_id: "tool-next",
            deadline_at: "2026-10-07T13:00:00Z",
          }),
        ]}
        runInfo={runInfo}
        pending
        approvalPendingToolId={null}
        approvalError={null}
        onApprovalDecision={vi.fn()}
      />
    )
    expect(screen.getByText(/Deadline/).closest("time")).toHaveAttribute(
      "dateTime",
      "2026-10-07T13:00:00Z"
    )
  })
})
