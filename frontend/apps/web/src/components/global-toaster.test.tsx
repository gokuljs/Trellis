import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { toast } from "sonner"

import "@trellis/ui/globals.css"
import { GlobalToaster } from "@/components/global-toaster"
import { ThemeProvider } from "@/components/theme-provider"

afterEach(() => {
  act(() => toast.dismiss())
  cleanup()
})

describe("GlobalToaster", () => {
  it("uses Trellis's animated mark for information notifications", async () => {
    render(
      <ThemeProvider defaultTheme="dark">
        <GlobalToaster />
      </ThemeProvider>
    )

    act(() => {
      toast.info("Information saved")
    })

    const title = await screen.findByText("Information saved")
    const notification = title.closest("[data-sonner-toast]")

    expect(
      notification?.querySelector("svg.trellis-mark--animated")
    ).toBeInTheDocument()
  })
})
