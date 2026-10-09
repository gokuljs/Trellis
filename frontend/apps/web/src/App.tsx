import { AppShell } from "@/components/app-shell"
import { AuthGate } from "@/components/auth-gate"
import { GlobalToaster } from "@/components/global-toaster"

export function App() {
  return (
    <>
      <AuthGate>
        <AppShell />
      </AuthGate>
      <GlobalToaster />
    </>
  )
}
