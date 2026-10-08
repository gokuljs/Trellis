import { useId } from "react"

import ditherProfile from "@/assets/dither-profile.png"
import "./trellis-mark.css"

type TrellisMarkProps = {
  animated?: boolean
  className?: string
  size?: number
}

export function TrellisMark({
  animated = false,
  className,
  size = 16,
}: TrellisMarkProps) {
  const instanceId = useId().replace(/:/g, "")
  const filterId = `trellis-mark-filter-${instanceId}`
  const maskId = `trellis-mark-mask-${instanceId}`
  const classes = [
    "trellis-mark",
    animated && "trellis-mark--animated",
    className,
  ]
    .filter(Boolean)
    .join(" ")

  return (
    <svg
      aria-hidden="true"
      className={classes}
      viewBox="62 50 280 225"
      width={size}
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <filter colorInterpolationFilters="sRGB" id={filterId}>
          <feComponentTransfer>
            <feFuncR intercept="-0.2" slope="4" type="linear" />
            <feFuncG intercept="-0.2" slope="4" type="linear" />
            <feFuncB intercept="-0.2" slope="4" type="linear" />
          </feComponentTransfer>
        </filter>
        <mask
          className="trellis-mark__mask"
          height="308"
          id={maskId}
          maskContentUnits="userSpaceOnUse"
          maskUnits="userSpaceOnUse"
          width="406"
          x="0"
          y="0"
        >
          <image
            filter={`url(#${filterId})`}
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
        mask={`url(#${maskId})`}
        width="406"
        x="0"
        y="0"
      />
      <text
        className="trellis-mark__eye"
        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        fontSize="8"
        fontWeight="600"
        x="232"
        y="101"
      >
        0
      </text>
      {animated ? (
        <path className="trellis-mark__eyelid" d="M 221 101 H 243" />
      ) : null}
      <text
        className="trellis-mark__eye"
        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        fontSize="8"
        fontWeight="600"
        x="306"
        y="101"
      >
        0
      </text>
      {animated ? (
        <path
          className="trellis-mark__eyelid trellis-mark__eyelid--right"
          d="M 295 101 H 317"
        />
      ) : null}
    </svg>
  )
}
