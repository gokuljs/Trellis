import { useEffect, useRef, useState } from "react"
import { Pause, Play } from "lucide-react"

import { mountLandscape, type LandscapeStatus } from "@/lib/dither-landscape"

export function DitherLandscape() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const controllerRef = useRef<ReturnType<typeof mountLandscape> | null>(null)
  const [status, setStatus] = useState<LandscapeStatus>("loading")

  useEffect(() => {
    if (!canvasRef.current) return
    const controller = mountLandscape(canvasRef.current, setStatus)
    controllerRef.current = controller
    return () => {
      controller.dispose()
      controllerRef.current = null
    }
  }, [])

  return (
    <>
      <div className="home-art" aria-hidden="true">
        <canvas ref={canvasRef} className="home-landscape" />
      </div>
      {status === "playing" || status === "paused" ? (
        <button
          className="home-motion-toggle"
          type="button"
          onClick={() => controllerRef.current?.togglePaused()}
        >
          {status === "playing" ? (
            <Pause size={12} aria-hidden="true" />
          ) : (
            <Play size={12} aria-hidden="true" />
          )}
          {status === "playing" ? "Pause animation" : "Resume animation"}
        </button>
      ) : null}
    </>
  )
}
