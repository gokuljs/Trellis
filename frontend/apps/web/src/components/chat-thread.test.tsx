import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ChatThread } from "@/components/chat-thread"
import type { Message, Session } from "@/lib/app-types"

const session: Session = {
  id: "session-1",
  title: "Trip plan",
  created_at: "2026-10-03T10:00:00Z",
  updated_at: "2026-10-03T10:00:00Z",
  message_count: 1,
  workspace_path: null,
}

function message(content: string): Message {
  return {
    id: "message-1",
    turn_id: "turn-1",
    role: "assistant",
    content,
    provider: "openai",
    model: "gpt-5.5",
    created_at: "2026-10-03T10:00:01Z",
  }
}

function renderThread(content: string) {
  return render(
    <ChatThread
      session={session}
      messages={[message(content)]}
      pending={false}
      streamingText={null}
      error={null}
      canRetry={false}
      canCancel={false}
      cancellationPending={false}
      onRetry={() => undefined}
      onCancel={() => undefined}
    />
  )
}

afterEach(cleanup)

describe("chat Markdown", () => {
  it("identifies a user message to assistive technology when its label is visually hidden", () => {
    render(
      <ChatThread
        session={session}
        messages={[{ ...message("A question"), role: "user", model: null }]}
        pending={false}
        streamingText={null}
        error={null}
        canRetry={false}
        canCancel={false}
        cancellationPending={false}
        onRetry={() => undefined}
        onCancel={() => undefined}
      />
    )

    expect(
      screen.getByRole("article", { name: "Your message" })
    ).toBeInTheDocument()
  })

  it("renders headings, paragraphs, emphasis, and lists from saved messages", () => {
    renderThread("## Trip plan\n\nA **useful** route.\n\n- Visit the mall")

    expect(
      screen.getByRole("heading", { level: 2, name: "Trip plan" })
    ).toBeInTheDocument()
    expect(screen.getByRole("paragraph")).toHaveTextContent("A useful route.")
    expect(screen.getByText("useful").tagName).toBe("STRONG")
    expect(screen.getByRole("listitem")).toHaveTextContent("Visit the mall")
  })

  it("renders GFM tables as semantic table content", () => {
    renderThread("| Day | Stop |\n| --- | --- |\n| 1 | Dubai Mall |")

    expect(screen.getByRole("table")).toBeInTheDocument()
    expect(
      screen.getByRole("columnheader", { name: "Day" })
    ).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "Dubai Mall" })).toBeInTheDocument()
  })

  it("renders links and code blocks as semantic Markdown elements", () => {
    renderThread(
      "See [the details](https://example.test) and `check-in`.\n\n```txt\nGate opens at 9:00\n```"
    )

    expect(screen.getByRole("link", { name: "the details" })).toHaveAttribute(
      "href",
      "https://example.test"
    )
    expect(screen.getByText("check-in").tagName).toBe("CODE")
    expect(screen.getByText("Gate opens at 9:00").tagName).toBe("CODE")
  })

  it("renders HTTP(S) images and omits unsafe or relative image URLs", () => {
    renderThread(
      "![Burj Khalifa](https://images.example.test/burj.jpg)\n\n![Unsafe](javascript:alert%281%29)\n\n![Local](../private.png)"
    )

    expect(screen.getByRole("img", { name: "Burj Khalifa" })).toHaveAttribute(
      "src",
      "https://images.example.test/burj.jpg"
    )
    expect(screen.queryByRole("img", { name: "Unsafe" })).toBeNull()
    expect(screen.queryByRole("img", { name: "Local" })).toBeNull()
  })

  it("does not turn raw HTML in a message into executable elements", () => {
    const { container } = renderThread(
      "<script>alert('xss')</script>\n\n<b>not raw HTML</b>"
    )

    expect(container.querySelector("script, b")).toBeNull()
  })

  it("renders Markdown while an assistant response is streaming", () => {
    render(
      <ChatThread
        session={session}
        messages={[]}
        pending
        streamingText="### Still working\n\nThe answer is **in progress**."
        error={null}
        canRetry={false}
        canCancel={false}
        cancellationPending={false}
        onRetry={() => undefined}
        onCancel={() => undefined}
      />
    )

    expect(
      screen.getByRole("heading", { level: 3, name: /Still working/ })
    ).toBeInTheDocument()
    expect(screen.getByText("in progress").tagName).toBe("STRONG")
  })
})
