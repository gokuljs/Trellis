import { Component, type ReactNode } from "react"

import "./workspace-bootstrap.css"

export class RootErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <main
        className="onboarding-shell onboarding-loading-shell"
        data-theme="dark"
      >
        <section
          className="workspace-bootstrap-error"
          aria-labelledby="root-error-heading"
        >
          <h1 id="root-error-heading">Trellis could not continue</h1>
          <p role="alert">Reload Trellis to reopen your workspace.</p>
          <button
            className="onboarding-primary-action"
            type="button"
            onClick={() => window.location.reload()}
          >
            Reload Trellis
          </button>
        </section>
      </main>
    )
  }
}
