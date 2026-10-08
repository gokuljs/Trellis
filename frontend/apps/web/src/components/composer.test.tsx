import type { ReactNode } from "react"
import { cleanup, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ThemeProvider } from "@/components/theme-provider"
import { Composer } from "@/components/composer"

vi.mock("metal-fx", () => ({
  MetalFx: ({
    children,
    disableGlow,
    innerShadow,
    paused,
    preset,
    strength,
    theme,
    variant,
  }: {
    children: ReactNode
    disableGlow?: boolean
    innerShadow?: boolean
    paused?: boolean
    preset?: string
    strength?: number
    theme?: string
    variant?: string
  }) => (
    <div
      data-testid="metal-send-shell"
      data-inner-shadow={String(Boolean(innerShadow))}
      data-disable-glow={String(Boolean(disableGlow))}
      data-paused={String(Boolean(paused))}
      data-preset={preset}
      data-strength={String(strength)}
      data-theme={theme}
      data-variant={variant}
    >
      {children}
    </div>
  ),
  useMetalBend: () => undefined,
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderComposer({
  theme = "dark",
  value = "",
}: {
  theme?: "dark" | "light"
  value?: string
} = {}) {
  const onSubmit = vi.fn()

  render(
    <ThemeProvider defaultTheme={theme}>
      <Composer
        value={value}
        placeholder="Write a message"
        workspacePath={null}
        workspaceSessionKey="new-session"
        onChange={vi.fn()}
        onSubmit={onSubmit}
        onPickWorkspace={async () => false}
        onSaveWorkspace={async () => undefined}
        onRemoveWorkspace={async () => undefined}
        budgetPreset="conservative"
        onBudgetChange={vi.fn()}
      />
    </ThemeProvider>
  )

  return { onSubmit }
}

describe("Composer send button", () => {
  it("keeps an upward arrow visible while the empty draft stays disabled", () => {
    renderComposer()

    const sendButton = screen.getByRole("button", { name: "Send" })
    expect(sendButton).toBeDisabled()
    expect(sendButton.querySelector("svg.lucide-arrow-up")).toBeInTheDocument()
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-paused",
      "true"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-disable-glow",
      "true"
    )
  })

  it("uses the chromatic circle in the app theme and submits through the wrapped button", async () => {
    const { onSubmit } = renderComposer({ theme: "light", value: "Draft" })
    const sendButton = screen.getByRole("button", { name: "Send" })

    expect(sendButton).toBeEnabled()
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-preset",
      "chromatic"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-variant",
      "circle"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-strength",
      "1"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-inner-shadow",
      "true"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-theme",
      "light"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-paused",
      "false"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-disable-glow",
      "false"
    )

    await userEvent.click(sendButton)
    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it("keeps Enter submission working with the metal button", async () => {
    const { onSubmit } = renderComposer({ value: "Draft" })

    await userEvent.type(
      screen.getByPlaceholderText("Write a message"),
      "{Enter}"
    )

    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it("pauses the ring for a ready draft when reduced motion is preferred", () => {
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          matches: query === "(prefers-reduced-motion: reduce)",
          media: query,
          onchange: null,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          addListener: vi.fn(),
          removeListener: vi.fn(),
          dispatchEvent: vi.fn(),
        }) as MediaQueryList
    )

    renderComposer({ value: "Draft" })

    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-paused",
      "true"
    )
    expect(screen.getByTestId("metal-send-shell")).toHaveAttribute(
      "data-disable-glow",
      "true"
    )
  })
})
