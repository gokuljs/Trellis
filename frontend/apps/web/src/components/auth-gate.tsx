import type { ReactNode } from "react"

import { TrellisMark } from "@/components/trellis-mark"
import { useAuth } from "@/lib/use-auth"
import type { AuthProvider } from "@/lib/auth-controller"
import "./auth-screen.css"

function ProviderMark({ provider }: { provider: AuthProvider }) {
  return provider === "google" ? (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
      <path
        fill="#4285F4"
        d="M21.6 12.23c0-.71-.06-1.39-.18-2.05H12v3.88h5.38a4.6 4.6 0 0 1-2 3.02v2.51h3.24c1.9-1.75 2.98-4.33 2.98-7.36Z"
      />
      <path
        fill="#34A853"
        d="M12 22c2.7 0 4.96-.9 6.62-2.41l-3.24-2.51c-.9.6-2.05.96-3.38.96-2.6 0-4.8-1.76-5.59-4.12H3.07v2.6A10 10 0 0 0 12 22Z"
      />
      <path
        fill="#FBBC05"
        d="M6.41 13.92A6 6 0 0 1 6.1 12c0-.67.11-1.32.31-1.92v-2.6H3.07A10 10 0 0 0 2 12c0 1.61.38 3.14 1.07 4.52l3.34-2.6Z"
      />
      <path
        fill="#EA4335"
        d="M12 5.96c1.47 0 2.79.51 3.83 1.51l2.87-2.87A9.62 9.62 0 0 0 12 2a10 10 0 0 0-8.93 5.48l3.34 2.6A5.98 5.98 0 0 1 12 5.96Z"
      />
    </svg>
  ) : (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="currentColor"
    >
      <path d="M12 .75a11.25 11.25 0 0 0-3.56 21.92c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.68.08-.68 1.13.08 1.73 1.16 1.73 1.16 1 1.72 2.63 1.22 3.27.93.1-.72.39-1.22.71-1.5-2.5-.28-5.12-1.25-5.12-5.56 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.1 1.16a10.78 10.78 0 0 1 5.64 0c2.15-1.45 3.09-1.16 3.09-1.16.61 1.55.23 2.7.11 2.98.72.79 1.16 1.79 1.16 3.02 0 4.32-2.63 5.28-5.14 5.56.4.35.77 1.03.77 2.08v3.08c0 .3.2.65.77.54A11.25 11.25 0 0 0 12 .75Z" />
    </svg>
  )
}

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
                      <ProviderMark provider={provider} />
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
