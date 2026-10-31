type FrameScheduler = {
  request: (callback: FrameRequestCallback) => number
  cancel: (id: number) => void
}

export function createLandscapeLoop(
  draw: (time: number) => void,
  scheduler: FrameScheduler = {
    request: (callback) => window.requestAnimationFrame(callback),
    cancel: (id) => window.cancelAnimationFrame(id),
  }
) {
  let frame: number | null = null
  let running = false
  let previous: number | null = null
  let painted: number | null = null
  let elapsed = 0

  const tick = (timestamp: number) => {
    frame = null
    if (!running) return
    if (previous !== null) elapsed += Math.min(timestamp - previous, 100) / 1000
    previous = timestamp
    if (painted === null || timestamp - painted >= 1000 / 30) {
      draw(elapsed)
      painted = timestamp
    }
    if (running) frame = scheduler.request(tick)
  }

  return {
    start() {
      if (running) return
      running = true
      frame = scheduler.request(tick)
    },
    stop() {
      running = false
      if (frame !== null) scheduler.cancel(frame)
      frame = null
      previous = null
      painted = null
    },
    drawStill() {
      draw(elapsed)
    },
  }
}
