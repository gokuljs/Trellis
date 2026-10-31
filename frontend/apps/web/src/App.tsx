import { AppShell } from "@/components/app-shell"
import { AuthGate } from "@/components/auth-gate"
import { AccountControl } from "@/components/account-control"
import { GlobalToaster } from "@/components/global-toaster"
import { HomePage } from "@/components/home-page"

export function App() {
  return (
    <>
      <AuthGate publicHome={<HomePage />}>
        <AppShell
          accountControl={(profile) => <AccountControl profile={profile} />}
        />
      </AuthGate>
      <GlobalToaster />
    </>
  )
}
