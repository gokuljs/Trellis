import type { ReactNode } from "react"

import { GitHubIcon } from "@trellis/ui/icons/github-icon"
import { GoogleIcon } from "@trellis/ui/icons/google-icon"
import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { useAuth } from "@/lib/use-auth"
import "./auth-screen.css"

export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth()

  if (auth.status === "signed-in" && auth.user) {
    return (
      <div className="authenticated-workspace" key={auth.user.id}>
        {children}
      </div>
    )
  }

  const configured = auth.status !== "configuration-error"
  const loading = auth.status === "loading"

  return (
    <main className="auth-screen">
      <div className="auth-brand">
        <TrellisMark size={28} />
        <span>Trellis</span>
      </div>
      <section className="auth-panel" aria-labelledby="auth-heading">
        {loading ? (
          <>
            <h1 className="sr-only" id="auth-heading">
              Signing in to Trellis
            </h1>
            <p className="auth-status" role="status">
              Checking your session…
            </p>
          </>
        ) : (
          <>
            <h1 id="auth-heading">
              {configured ? "Sign in to Trellis" : "Sign-in needs setup"}
            </h1>
            <p className="auth-description">
              {configured
                ? "Use your Google or GitHub account to get started."
                : "Add your Supabase project configuration, then restart the frontend."}
            </p>
            {auth.error ? (
              <p className="auth-error" role="alert">
                {auth.error}
              </p>
            ) : null}
            {configured ? (
              <div
                className="auth-providers"
                role="group"
                aria-label="Sign-in options"
              >
                {(["google", "github"] as const).map((provider) => {
                  const label = provider === "google" ? "Google" : "GitHub"
                  return (
                    <button
                      className="auth-provider"
                      key={provider}
                      type="button"
                      disabled={auth.pendingProvider !== null}
                      onClick={() => {
                        void auth.signIn(provider)
                      }}
                    >
                      {provider === "google" ? <GoogleIcon /> : <GitHubIcon />}
                      {auth.pendingProvider === provider
                        ? `Connecting to ${label}…`
                        : `Continue with ${label}`}
                    </button>
                  )
                })}
              </div>
            ) : null}
            {auth.pendingProvider ? (
              <p className="sr-only" role="status">
                Connecting to{" "}
                {auth.pendingProvider === "google" ? "Google" : "GitHub"}…
              </p>
            ) : null}
            {auth.status === "error" ? (
              <button
                className="auth-retry"
                type="button"
                onClick={() => {
                  void auth.retry()
                }}
              >
                Try again
              </button>
            ) : null}
          </>
        )}
      </section>
      <p className="auth-footer">
        Your account opens your local Trellis workspace.
      </p>
    </main>
  )
}
