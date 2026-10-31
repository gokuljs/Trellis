import assert from "node:assert/strict"
import { test } from "bun:test"

import { createLandscapeLoop } from "./dither-landscape-loop"
import { arcGeometry, mountLandscape } from "./dither-landscape"

function frameScheduler() {
  const frames = new Map()
  let id = 0
  return {
    request(callback) {
      frames.set(++id, callback)
      return id
    },
    cancel(frame) {
      frames.delete(frame)
    },
    tick(time) {
      const pending = [...frames.values()]
      frames.clear()
      for (const callback of pending) callback(time)
    },
    get pending() {
      return frames.size
    },
  }
}

test("landscape frames are capped and resume without advancing through a pause", () => {
  const scheduler = frameScheduler()
  const times = []
  const loop = createLandscapeLoop((time) => times.push(time), scheduler)
  loop.start()
  loop.start()
  assert.equal(scheduler.pending, 1)
  for (let time = 0; time < 1000; time += 1000 / 120) scheduler.tick(time)
  assert.ok(times.length >= 59 && times.length <= 61)
  const beforePause = times.at(-1)
  loop.stop()
  assert.equal(scheduler.pending, 0)
  loop.start()
  scheduler.tick(10000)
  assert.ok(times.at(-1) - beforePause < 0.1)
  loop.stop()
})

test("the frame deadline preserves sixty draws per second across display cadences", () => {
  for (const refreshRate of [60, 120, 125]) {
    const scheduler = frameScheduler()
    let draws = 0
    const loop = createLandscapeLoop(() => draws++, scheduler)
    loop.start()
    for (let frame = 0; frame < refreshRate * 3; frame++) {
      scheduler.tick((frame * 1000) / refreshRate)
    }
    assert.ok(
      draws >= 179 && draws <= 181,
      `${refreshRate}Hz rendered ${draws} frames`
    )
    loop.stop()
  }
})

function withLandscape(options, run) {
  const scheduler = frameScheduler()
  const media = Object.assign(new EventTarget(), { matches: !!options.reduced })
  const document = Object.assign(new EventTarget(), { hidden: false })
  const window = Object.assign(new EventTarget(), {
    devicePixelRatio: 1,
    matchMedia: () => media,
    requestAnimationFrame(callback) {
      assert.equal(this, window)
      return scheduler.request(callback)
    },
    cancelAnimationFrame(id) {
      assert.equal(this, window)
      scheduler.cancel(id)
    },
  })
  const canvas = Object.assign(new EventTarget(), {
    width: 0,
    height: 0,
    dataset: {},
    getBoundingClientRect: () =>
      options.initialSize ?? { width: 320, height: 240 },
    getContext: (kind) => (kind === "webgl2" && !options.noWebGL ? gl : null),
  })
  const counts = {
    draws: 0,
    programs: 0,
    deletedPrograms: 0,
    buffers: 0,
    deletedBuffers: 0,
    deletedShaders: 0,
    vertexArrays: 0,
    deletedVertexArrays: 0,
    posters: 0,
    particleDraws: 0,
    compiledShaders: 0,
    instances: 0,
  }
  const gl = {
    canvas,
    VERTEX_SHADER: 1,
    FRAGMENT_SHADER: 2,
    COMPILE_STATUS: 3,
    LINK_STATUS: 4,
    ARRAY_BUFFER: 5,
    STATIC_DRAW: 6,
    FLOAT: 7,
    TRIANGLES: 8,
    createProgram() {
      counts.programs++
      return {}
    },
    createBuffer() {
      counts.buffers++
      return {}
    },
    createShader: () => ({}),
    createVertexArray() {
      counts.vertexArrays++
      return {}
    },
    deleteVertexArray() {
      counts.deletedVertexArrays++
    },
    bindVertexArray() {},
    vertexAttribDivisor() {},
    enable() {},
    disable() {},
    blendFunc() {},
    drawArraysInstanced(_mode, _first, _vertices, instances) {
      counts.particleDraws++
      counts.draws++
      counts.instances = instances
    },
    shaderSource() {},
    compileShader() {
      counts.compiledShaders++
    },
    attachShader() {},
    linkProgram() {},
    getShaderParameter: () =>
      !options.shaderFailure &&
      counts.compiledShaders !== options.shaderFailureAt,
    getProgramParameter: () => true,
    getAttribLocation: () => 0,
    getUniformLocation: () => ({}),
    useProgram() {},
    bindBuffer() {},
    bufferData() {},
    enableVertexAttribArray() {},
    vertexAttribPointer() {},
    viewport() {},
    uniform2f() {},
    uniform4f() {},
    uniform1f() {},
    drawArrays() {
      counts.draws++
    },
    deleteBuffer() {
      counts.deletedBuffers++
    },
    deleteProgram() {
      counts.deletedPrograms++
    },
    deleteShader() {
      counts.deletedShaders++
    },
  }
  const poster = {
    width: 0,
    height: 0,
    getContext: () => ({
      createImageData: (width, height) => ({
        data: new Uint8ClampedArray(width * height * 4),
      }),
      putImageData() {
        counts.posters++
      },
    }),
  }
  const observers = {}
  const globals = {
    window,
    document,
    ResizeObserver: class {
      constructor(callback) {
        if (options.observerFailure) throw new Error("Observer unavailable")
        observers.resize = callback
      }
      observe() {}
      disconnect() {}
    },
    IntersectionObserver: class {
      constructor(callback) {
        observers.intersection = callback
      }
      observe() {}
      disconnect() {}
    },
  }
  const originals = Object.fromEntries(
    Object.keys(globals).map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ])
  )
  for (const [key, value] of Object.entries(globals))
    Object.defineProperty(globalThis, key, {
      configurable: true,
      writable: true,
      value,
    })
  const states = []
  let controller
  try {
    controller = mountLandscape(canvas, poster, (state) => states.push(state))
    run({
      controller,
      canvas,
      poster,
      media,
      document,
      scheduler,
      observers,
      counts,
      states,
    })
  } finally {
    controller?.dispose()
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
}

test("reduced motion renders a still frame and responds to preference changes", () => {
  withLandscape(
    { reduced: true },
    ({ canvas, media, scheduler, counts, states }) => {
      assert.equal(states.at(-1), "reduced")
      assert.ok(counts.draws > 0)
      assert.equal(canvas.dataset.ready, "true")
      assert.equal(scheduler.pending, 0)
      media.matches = false
      media.dispatchEvent(new Event("change"))
      assert.equal(states.at(-1), "playing")
      assert.equal(scheduler.pending, 1)
      media.matches = true
      media.dispatchEvent(new Event("change"))
      assert.equal(scheduler.pending, 0)
    }
  )
})

test("pause, page visibility and intersection suspend rendering", () => {
  withLandscape(
    {},
    ({ controller, document, scheduler, observers, states }) => {
      controller.togglePaused()
      assert.equal(states.at(-1), "paused")
      assert.equal(scheduler.pending, 0)
      controller.togglePaused()
      assert.equal(scheduler.pending, 1)
      document.hidden = true
      document.dispatchEvent(new Event("visibilitychange"))
      assert.equal(scheduler.pending, 0)
      document.hidden = false
      document.dispatchEvent(new Event("visibilitychange"))
      assert.equal(scheduler.pending, 1)
      observers.intersection([{ isIntersecting: false }])
      assert.equal(scheduler.pending, 0)
      observers.intersection([{ isIntersecting: true }])
      assert.equal(scheduler.pending, 1)
    }
  )
})

test("context loss shows fallback and restoration rebuilds the renderer", () => {
  withLandscape({}, ({ canvas, scheduler, counts, states }) => {
    const lost = new Event("webglcontextlost", { cancelable: true })
    canvas.dispatchEvent(lost)
    assert.equal(lost.defaultPrevented, true)
    assert.equal(canvas.dataset.ready, "false")
    assert.equal(states.at(-1), "unavailable")
    assert.equal(scheduler.pending, 0)
    canvas.dispatchEvent(new Event("webglcontextrestored"))
    assert.equal(counts.programs, 6)
    assert.equal(canvas.dataset.ready, "true")
    assert.equal(states.at(-1), "playing")
    assert.equal(scheduler.pending, 1)
  })
})

test("disposal releases GPU resources and ignores already queued observer callbacks", () => {
  withLandscape({}, ({ controller, scheduler, observers, counts, states }) => {
    controller.dispose()
    const draws = counts.draws
    const updates = states.length
    observers.resize()
    observers.intersection([{ isIntersecting: true }])
    scheduler.tick(1000)
    assert.equal(counts.draws, draws)
    assert.equal(states.length, updates)
    assert.equal(scheduler.pending, 0)
    assert.equal(counts.deletedBuffers, 1)
    assert.equal(counts.deletedPrograms, 3)
    assert.equal(counts.deletedShaders, 6)
    assert.equal(counts.deletedVertexArrays, 2)
  })
})

test("unsupported WebGL2 and shader or setup failures keep the rendered poster", () => {
  for (const options of [
    { noWebGL: true },
    { shaderFailure: true },
    { shaderFailureAt: 5 },
    { observerFailure: true },
  ]) {
    withLandscape(options, ({ canvas, scheduler, counts, states }) => {
      assert.equal(states.at(-1), "unavailable")
      assert.notEqual(canvas.dataset.ready, "true")
      assert.equal(counts.posters, 1)
      assert.equal(scheduler.pending, 0)
      assert.equal(counts.deletedBuffers, counts.buffers)
      assert.equal(counts.deletedPrograms, counts.programs)
      assert.equal(counts.deletedVertexArrays, counts.vertexArrays)
    })
  }
})

test("fallback resize bursts render one new poster and identical sizes keep the bitmap", () => {
  withLandscape(
    { noWebGL: true },
    ({ canvas, scheduler, observers, counts }) => {
      assert.equal(counts.posters, 1)
      canvas.getBoundingClientRect = () => ({ width: 420, height: 240 })
      observers.resize()
      observers.resize()
      observers.resize()
      assert.equal(scheduler.pending, 1)
      scheduler.tick(0)
      assert.equal(counts.posters, 2)
      observers.resize()
      scheduler.tick(20)
      assert.equal(counts.posters, 2)
    }
  )
})

test("effect cleanup and setup can reuse the canvas without losing its context", () => {
  withLandscape({}, ({ controller, canvas, poster, scheduler, counts }) => {
    controller.dispose()
    const states = []
    const replay = mountLandscape(canvas, poster, (status) =>
      states.push(status)
    )
    try {
      assert.equal(states.at(-1), "playing")
      assert.equal(canvas.dataset.ready, "true")
      assert.equal(scheduler.pending, 1)
      assert.equal(counts.programs, 6)
    } finally {
      replay.dispose()
    }
    assert.equal(counts.deletedPrograms, 6)
    assert.equal(counts.deletedBuffers, 2)
    assert.equal(scheduler.pending, 0)
  })
})

test("arc geometry meets the viewport edges and places the colored region below the rim", () => {
  const scene = arcGeometry(1280, 720)
  const bottom = 0.4 * 1280 + 8 * scene.scale
  assert.ok(
    Math.abs(
      Math.hypot(bottom - scene.centerX, 720 - scene.centerY) - scene.radius
    ) < 0.001
  )
  assert.ok(
    Math.abs(
      Math.hypot(1280 - scene.centerX, 720 * 0.415 - scene.centerY) -
        scene.radius
    ) < 0.001
  )
  assert.ok(
    Math.hypot(1280 - scene.centerX, 720 - scene.centerY) > scene.radius
  )
  assert.ok(Math.hypot(160 - scene.centerX, 160 - scene.centerY) < scene.radius)
  assert.equal(scene.particleCount, 3446)
})

test("each frame draws the colored field, instanced particles and grain", () => {
  withLandscape({}, ({ counts, scheduler }) => {
    const draws = counts.draws
    const particles = counts.particleDraws
    scheduler.tick(0)
    assert.equal(counts.draws - draws, 3)
    assert.equal(counts.particleDraws - particles, 1)
    assert.equal(counts.programs, 3)
  })
})

test("a zero-size initial canvas recovers when layout supplies its dimensions", () => {
  withLandscape(
    { initialSize: { width: 0, height: 0 } },
    ({ canvas, counts, observers, scheduler, states }) => {
      assert.equal(counts.instances, 0)
      assert.equal(states.at(-1), "playing")
      canvas.getBoundingClientRect = () => ({ width: 320, height: 240 })
      observers.resize()
      scheduler.tick(0)
      assert.ok(counts.instances > 0)
      assert.equal(canvas.width, 320)
      assert.equal(canvas.height, 240)
      assert.equal(canvas.dataset.ready, "true")
      assert.equal(counts.posters, 2)
    }
  )
})
