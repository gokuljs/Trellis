type TrellisMarkProps = {
  size?: number
}

export function TrellisMark({ size = 16 }: TrellisMarkProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 16 16"
      width={size}
    >
      <rect
        height="13.5"
        stroke="currentColor"
        strokeWidth="1.25"
        width="13.5"
        x="1.25"
        y="1.25"
      />
      <path
        d="M5.25 1.25v13.5M10.75 1.25v13.5M1.25 8h13.5"
        stroke="currentColor"
        strokeWidth="1.25"
      />
    </svg>
  )
}
