import { CalendarClock, FileText, GitBranch, Search } from "lucide-react";

import type { WorkspaceView } from "@/lib/app-types";
import "./welcome-panel.css";

type WelcomePanelProps = {
  activeView: WorkspaceView;
  activeSessionTitle: string | null;
};

const capabilityItems = [
  { label: "Trace a codebase", icon: GitBranch },
  { label: "Search the web", icon: Search },
  { label: "Make artifacts", icon: FileText },
  { label: "Schedule work", icon: CalendarClock },
];

function CapabilityGrid() {
  return (
    <div className="capability-grid">
      {capabilityItems.map(({ label, icon: Icon }) => (
        <div key={label}>
          <Icon size={16} aria-hidden="true" />
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function DitherFace() {
  return (
    <svg
      aria-hidden="true"
      className="welcome-dither"
      viewBox="0 0 192 176"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <pattern
          height="11"
          id="welcome-dither-pattern"
          patternUnits="userSpaceOnUse"
          width="9"
        >
          <text
            fill="currentColor"
            fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
            fontSize="8"
            fontWeight="600"
            x="0"
            y="8"
          >
            0
          </text>
        </pattern>
      </defs>
      <path
        d="M97 19c-34 0-58 24-58 58-10 5-13 17-8 27 3 6 7 9 13 12 2 21 16 36 38 44l-1 9h38l-1-12c22-9 35-28 36-54 9-5 12-17 7-25-2-35-26-59-64-59Z"
        fill="url(#welcome-dither-pattern)"
      />
      <path
        d="M132 34c15 11 23 28 23 49 5 7 7 15 5 22-3 3-6 5-10 7-2 23-12 38-28 48 17-14 24-34 22-57 8-5 10-14 5-21-1-19-6-36-17-48Z"
        fill="var(--shell-bg)"
        opacity="0.2"
      />
      <g className="welcome-dither__eye">
        <ellipse
          cx="72"
          cy="83"
          fill="var(--shell-bg)"
          rx="10"
          ry="4.5"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <circle cx="72" cy="83" fill="currentColor" r="2.2" />
      </g>
      <g className="welcome-dither__eye welcome-dither__eye--right">
        <ellipse
          cx="119"
          cy="83"
          fill="var(--shell-bg)"
          rx="10"
          ry="4.5"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <circle cx="119" cy="83" fill="currentColor" r="2.2" />
      </g>
      <path
        d="M96 87c-2 12-5 19-8 25 4 3 9 4 14 2m-17 10c7 5 17 5 25 0"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </svg>
  );
}

export function WelcomePanel({
  activeView,
  activeSessionTitle,
}: WelcomePanelProps) {
  if (activeView === "Capabilities") {
    return (
      <div className="utility-panel">
        <div className="utility-kicker">AGENT CAPABILITIES</div>
        <h1>Build with an agent that can follow the thread.</h1>
        <p>
          Research, code, inspect files, and keep a working context across every
          session.
        </p>
        <CapabilityGrid />
      </div>
    );
  }

  if (activeView === "session") {
    return (
      <div className="utility-panel compact">
        <div className="utility-kicker">SESSION</div>
        <h1>{activeSessionTitle ?? "New session"}</h1>
        <p>Trellis is ready for the next piece of work.</p>
      </div>
    );
  }

  return (
    <div className="welcome-scene">
      <DitherFace />
      <p className="welcome-scene__copy">What would you like to work on?</p>
    </div>
  );
}
