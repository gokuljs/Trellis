import { AppShell } from "@/components/app-shell"
import { AuthGate } from "@/components/auth-gate"
import { AccountControl } from "@/components/account-control"
import { GlobalToaster } from "@/components/global-toaster"

export function App() {
  return (
    <>
      <AuthGate>
        <AppShell accountControl={<AccountControl />} />
      </AuthGate>
      <GlobalToaster />
    </>
  )
}
