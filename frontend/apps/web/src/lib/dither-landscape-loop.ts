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
  let deadline: number | null = null
  const interval = 1000 / 60
  let elapsed = 0

  const tick = (timestamp: number) => {
    frame = null
    if (!running) return
    if (previous !== null) elapsed += Math.min(timestamp - previous, 100) / 1000
    previous = timestamp
    if (deadline === null || timestamp >= deadline - 0.75) {
      draw(elapsed)
      deadline =
        deadline === null
          ? timestamp + interval
          : deadline +
            Math.max(1, Math.floor((timestamp - deadline) / interval) + 1) *
              interval
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
      deadline = null
    },
    drawStill() {
      draw(elapsed)
    },
  }
}
