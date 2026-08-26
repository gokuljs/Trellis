import {
  Check,
  CircleAlert,
  Info,
  LoaderCircle,
  TriangleAlert,
  X,
} from "lucide-react"
import { Toaster } from "sonner"

import { useTheme } from "@/components/theme-provider"

const iconProps = {
  size: 15,
  strokeWidth: 1.8,
  "aria-hidden": true,
} as const

export function GlobalToaster() {
  const { resolvedTheme } = useTheme()

  return (
    <Toaster
      customAriaLabel="Notifications"
      theme={resolvedTheme}
      position="top-right"
      expand
      visibleToasts={4}
      gap={9}
      duration={4200}
      closeButton
      offset={{ top: 18, right: 18 }}
      mobileOffset={{ top: 12, right: 12, left: 12 }}
      swipeDirections={["right"]}
      icons={{
        success: <Check {...iconProps} />,
        error: <CircleAlert {...iconProps} />,
        info: <Info {...iconProps} />,
        warning: <TriangleAlert {...iconProps} />,
        loading: <LoaderCircle {...iconProps} />,
        close: <X size={12} strokeWidth={1.8} aria-hidden="true" />,
      }}
      toastOptions={{
        closeButtonAriaLabel: "Dismiss notification",
        classNames: {
          toast: "trellis-toast",
          success: "trellis-toast-success",
          error: "trellis-toast-error",
          info: "trellis-toast-info",
          warning: "trellis-toast-warning",
          loading: "trellis-toast-loading",
          icon: "trellis-toast-icon",
          content: "trellis-toast-content",
          title: "trellis-toast-title",
          description: "trellis-toast-description",
          closeButton: "trellis-toast-close",
          actionButton: "trellis-toast-action",
        },
      }}
    />
  )
}
