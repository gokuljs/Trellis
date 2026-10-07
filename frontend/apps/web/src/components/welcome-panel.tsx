import type { WorkspaceView } from "@/lib/app-types";
import ditherProfile from "@/assets/dither-profile.png";
import "./welcome-panel.css";

type WelcomePanelProps = {
  activeView: WorkspaceView;
  activeSessionTitle: string | null;
};

function DitherFace() {
  return (
    <svg
      aria-hidden="true"
      className="welcome-dither"
      viewBox="62 50 280 225"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <filter colorInterpolationFilters="sRGB" id="welcome-dither-threshold">
          <feComponentTransfer>
            <feFuncR intercept="-0.2" slope="4" type="linear" />
            <feFuncG intercept="-0.2" slope="4" type="linear" />
            <feFuncB intercept="-0.2" slope="4" type="linear" />
          </feComponentTransfer>
        </filter>
        <mask
          className="welcome-dither__mask"
          height="308"
          id="welcome-dither-mask"
          maskContentUnits="userSpaceOnUse"
          maskUnits="userSpaceOnUse"
          width="406"
          x="0"
          y="0"
        >
          <image
            filter="url(#welcome-dither-threshold)"
            height="308"
            href={ditherProfile}
            width="406"
            x="0"
            y="0"
          />
        </mask>
      </defs>
      <rect
        fill="currentColor"
        height="308"
        mask="url(#welcome-dither-mask)"
        width="406"
        x="0"
        y="0"
      />
      <text
        className="welcome-dither__eye"
        fill="currentColor"
        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        fontSize="8"
        fontWeight="600"
        x="232"
        y="101"
      >
        0
      </text>
      <path className="welcome-dither__eyelid" d="M 221 101 H 243" />
      <text
        className="welcome-dither__eye"
        fill="currentColor"
        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        fontSize="8"
        fontWeight="600"
        x="306"
        y="101"
      >
        0
      </text>
      <path
        className="welcome-dither__eyelid welcome-dither__eyelid--right"
        d="M 295 101 H 317"
      />
    </svg>
  );
}

export function WelcomePanel({
  activeView,
  activeSessionTitle,
}: WelcomePanelProps) {
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
      <p className="welcome-scene__copy">What would you like to work on?</p>
      <DitherFace />
    </div>
  );
}
