import { useEffect, type ReactNode } from "react"

import { GitHubIcon } from "@trellis/ui/icons/github-icon"
import { GoogleIcon } from "@trellis/ui/icons/google-icon"
import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { useAuth } from "@/lib/use-auth"
import { navigate, parseAppRoute, useAppPath } from "@/lib/app-routes"
import {
  consumeAuthReturnPath,
  rememberAuthReturnPath,
} from "@/lib/auth-return"
import { SessionQueryProvider } from "@/components/session-query-provider"
import "./auth-screen.css"

function AuthPageLink({
  href,
  children,
}: {
  href: "/login" | "/signup"
  children: ReactNode
}) {
  return (
    <a
      href={href}
      onClick={(event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return
        }
        event.preventDefault()
        navigate(href)
      }}
    >
      {children}
    </a>
  )
}

export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const path = useAppPath()
  const authPage = path === "/login" || path === "/signup"
  const signedIn = auth.status === "signed-in" && auth.user !== null
  const signup = path === "/signup"

  useEffect(() => {
    // Let session restoration consume OAuth callback parameters first.
    if (auth.status === "loading") return
    const route = parseAppRoute(path)
    if (route.kind === "unknown") {
      navigate("/", true)
      return
    }
    if (route.path !== path) {
      navigate(route.path, true)
      return
    }
    if (signedIn && (authPage || route.kind === "home")) {
      const target = consumeAuthReturnPath()
      if (target || authPage) navigate(target ?? "/", true)
    } else if (!signedIn && !authPage) {
      if (route.kind === "chat") rememberAuthReturnPath(route.path)
      navigate("/login", true)
    }
  }, [auth.status, authPage, signedIn, path])

  if (signedIn && auth.user) {
    return (
      <div
        className="authenticated-workspace"
        key={`${auth.user.id}:${auth.sessionEpoch}`}
      >
        <SessionQueryProvider>{children}</SessionQueryProvider>
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
              {configured
                ? signup
                  ? "Create your Trellis account"
                  : "Sign in to Trellis"
                : "Sign-in needs setup"}
            </h1>
            <p className="auth-description">
              {configured
                ? signup
                  ? "Use your Google or GitHub account to create an account."
                  : "Use your Google or GitHub account to get started."
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
                aria-label={signup ? "Sign-up options" : "Sign-in options"}
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
            <p className="auth-switch">
              {signup ? "Already have an account? " : "New to Trellis? "}
              <AuthPageLink href={signup ? "/login" : "/signup"}>
                {signup ? "Sign in" : "Sign up"}
              </AuthPageLink>
            </p>
          </>
        )}
      </section>
      <p className="auth-footer">Your account opens your chats in Trellis.</p>
    </main>
  )
}
