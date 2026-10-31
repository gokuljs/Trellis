import type { ReactNode } from "react"

import { GitHubIcon } from "@trellis/ui/icons/github-icon"
import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { navigate, type AppPath } from "@/lib/app-routes"
import { DitherLandscape } from "./dither-landscape"
import "./home-page.css"

const sourceUrl = "https://github.com/gokuljs/Trellis"

function HomeLink({
  href,
  className,
  children,
}: {
  href: AppPath
  className?: string
  children: ReactNode
}) {
  return (
    <a
      href={href}
      className={className}
      onClick={(event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        )
          return
        event.preventDefault()
        navigate(href)
      }}
    >
      {children}
    </a>
  )
}

export function HomePage() {
  return (
    <div className="home-page">
      <header className="home-header">
        <div className="home-header-inner">
          <HomeLink href="/" className="home-brand">
            <TrellisMark size={20} />
            <span>Trellis</span>
          </HomeLink>
          <nav className="home-nav" aria-label="Main navigation">
            <HomeLink href="/login">Sign in</HomeLink>
            <a
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
              aria-label="Trellis on GitHub"
              title="Trellis on GitHub"
            >
              <GitHubIcon size={17} />
            </a>
          </nav>
        </div>
      </header>

      <main className="home-main">
        <div className="home-container">
          <section className="home-content" aria-labelledby="home-heading">
            <p className="home-eyebrow">Build with Trellis</p>
            <h1 id="home-heading">
              A team of agents
              <br />
              <span className="home-heading-muted">for coding</span>
              <br />
              <span className="home-heading-accent">and research</span>.
            </h1>
            <p className="home-description">
              A team of agents for coding and research. Set the direction and
              follow their work in one shared workspace.
            </p>
          </section>
        </div>
      </main>

      <footer className="home-footer">
        <div className="home-footer-brand">
          <TrellisMark size={20} />
          <span>Trellis</span>
        </div>
        <span className="home-copyright">Trellis © 2026</span>
      </footer>
      <DitherLandscape />
    </div>
  )
}
