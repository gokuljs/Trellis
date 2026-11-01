import { useEffect, useRef } from "react"

import { mountLandscape } from "@/lib/dither-landscape"

export function DitherLandscape() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const posterRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!canvasRef.current || !posterRef.current) return
    const controller = mountLandscape(canvasRef.current, posterRef.current)
    return () => {
      controller.dispose()
    }
  }, [])

  return (
    <div className="home-art" aria-hidden="true">
      <canvas ref={posterRef} className="home-landscape-poster" />
      <canvas ref={canvasRef} className="home-landscape" />
    </div>
  )
}
