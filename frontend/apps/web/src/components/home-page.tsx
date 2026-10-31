import { useState, type ReactNode } from "react"
import {
  ArrowRight,
  ArrowUpRight,
  Code2,
  Network,
  Search,
  type LucideIcon,
} from "lucide-react"

import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { navigate, type AppPath } from "@/lib/app-routes"
import { DitherLandscape } from "./dither-landscape"
import "./home-page.css"

const sourceUrl = "https://github.com/gokuljs/Trellis"

type Capability = {
  id: "coding" | "research" | "collaboration"
  label: string
  description: string
  Icon: LucideIcon
}

const capabilities: readonly Capability[] = [
  {
    id: "coding",
    label: "Coding",
    description:
      "Explore an idea, work through a problem, and build with a team of agents.",
    Icon: Code2,
  },
  {
    id: "research",
    label: "Research",
    description:
      "Ask a bigger question. Follow the research and go straight to the sources.",
    Icon: Search,
  },
  {
    id: "collaboration",
    label: "Collaboration",
    description:
      "Give your agents a direction and follow their work in one shared conversation.",
    Icon: Network,
  },
]

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
  const [activeCapability, setActiveCapability] =
    useState<Capability["id"]>("coding")
  const capability = capabilities.find((item) => item.id === activeCapability)!

  return (
    <div className="home-page">
      <header className="home-header">
        <HomeLink href="/" className="home-brand">
          <TrellisMark size={28} />
          <span>Trellis</span>
        </HomeLink>
        <nav className="home-nav" aria-label="Main navigation">
          <a href={sourceUrl} target="_blank" rel="noreferrer">
            Source <ArrowUpRight size={14} aria-hidden="true" />
          </a>
          <HomeLink href="/login" className="home-sign-in">
            Sign in
          </HomeLink>
        </nav>
      </header>

      <main className="home-main">
        <section className="home-content" aria-labelledby="home-heading">
          <p className="home-eyebrow">Coding + research, together</p>
          <h1 id="home-heading">
            Think bigger.
            <span>Build together.</span>
          </h1>
          <p className="home-description">
            A team of agents for coding and research. Give them a direction.
            Follow the work as it takes shape.
          </p>
          <div className="home-actions">
            <HomeLink href="/app" className="home-primary-link">
              Open Trellis <ArrowRight size={18} aria-hidden="true" />
            </HomeLink>
            <a
              className="home-source-link"
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
            >
              View source <ArrowUpRight size={16} aria-hidden="true" />
            </a>
          </div>

          <div className="home-capabilities">
            <div
              className="home-capability-options"
              role="group"
              aria-label="Explore Trellis"
            >
              {capabilities.map(({ id, label, Icon }) => (
                <button
                  type="button"
                  key={id}
                  aria-pressed={activeCapability === id}
                  aria-controls="home-capability-description"
                  onClick={() => setActiveCapability(id)}
                >
                  <Icon size={15} aria-hidden="true" />
                  {label}
                </button>
              ))}
            </div>
            <p id="home-capability-description" aria-live="polite">
              {capability.description}
            </p>
          </div>
        </section>
      </main>

      <footer className="home-footer">
        <span>Your ideas. Your direction.</span>
        <a href={sourceUrl} target="_blank" rel="noreferrer">
          Built in the open <ArrowUpRight size={13} aria-hidden="true" />
        </a>
      </footer>
      <DitherLandscape />
    </div>
  )
}
