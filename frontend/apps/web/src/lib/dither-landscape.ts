import { createLandscapeLoop } from "./dither-landscape-loop"

const TAU = Math.PI * 2
const palette = {
  ground: [10, 9, 11],
  rim: [137, 160, 255],
  pink: [247, 121, 203],
  plum: [96, 11, 47],
  particle: [251, 250, 252],
} as const

export type ArcGeometry = {
  width: number
  height: number
  scale: number
  centerX: number
  centerY: number
  radius: number
  angleStart: number
  angleEnd: number
  depth: number
  particleCount: number
}

export function arcGeometry(width: number, height: number): ArcGeometry {
  const scale = Math.min(
    2,
    Math.max(0.5, Math.sqrt((width * height) / 2073600))
  )
  const bottom = Math.min(width * 0.405, width * 0.4 + 8 * scale)
  const reach = width * 0.6 + 1492 * scale
  const rise = Math.min(
    height * 0.585,
    reach < 2500 * scale
      ? 2000 * scale - Math.sqrt((2500 * scale) ** 2 - reach ** 2)
      : Infinity
  )
  const horizontal = width - bottom
  const chord = Math.hypot(horizontal, rise)
  const sagitta = chord * 0.062
  const radius = chord ** 2 / (8 * sagitta) + sagitta / 2
  const centerX = (bottom + width) / 2 - (rise / chord) * (radius - sagitta)
  const centerY = height - rise / 2 - (horizontal / chord) * (radius - sagitta)
  const angles = [
    Math.atan2(-centerY, -centerX),
    Math.atan2(-centerY, width - centerX),
    Math.atan2(height - centerY, -centerX),
    Math.atan2(height - centerY, width - centerX),
  ]
  const angleStart = Math.min(...angles) - 0.04
  const angleEnd = Math.max(...angles) + 0.04
  const depth = 500 * scale
  const particleCount = Math.max(
    0,
    Math.min(
      30000,
      Math.round(
        0.00625 * (angleEnd - angleStart) * (radius - depth / 2) * depth
      )
    )
  )
  return {
    width,
    height,
    scale,
    centerX,
    centerY,
    radius,
    angleStart,
    angleEnd,
    depth,
    particleCount,
  }
}

function random(seed: number): number {
  const state = (Math.imul(seed, 747796405) + 2891336453) >>> 0
  const mixed =
    Math.imul((state >>> ((state >>> 28) + 4)) ^ state, 277803737) >>> 0
  return ((mixed >>> 22) ^ mixed) >>> 0
}

function unit(seed: number): number {
  return random(seed) / 4294967296
}

function particleData(scene: ArcGeometry): Float32Array {
  const data = new Float32Array(scene.particleCount * 7)
  for (let index = 0; index < scene.particleCount; index++) {
    const seed = random(index + 9173)
    const maximumDepth = Math.max(
      14 * scene.scale,
      scene.depth * (1 - Math.cbrt(unit(seed + 1)))
    )
    const frequency = 0.24 + unit(seed + 5) * 0.66
    data.set(
      [
        scene.angleStart + unit(seed) * (scene.angleEnd - scene.angleStart),
        maximumDepth,
        (8.4 + unit(seed + 2) * 13.2) * scene.scale,
        unit(seed + 3) * maximumDepth,
        (3.6 * scene.scale) / frequency,
        frequency,
        unit(seed + 4) * TAU,
      ],
      index * 7
    )
  }
  return data
}

function vignette(x: number, y: number, scene: ArcGeometry): number {
  return Math.max(
    0.2,
    1 -
      (Math.hypot(x - scene.width / 2, y - scene.height / 2) /
        (1600 * scene.scale)) **
        3
  )
}

function drawPoster(
  canvas: HTMLCanvasElement,
  scene: ArcGeometry,
  particles: Float32Array
) {
  const context = canvas.getContext("2d", { alpha: false })
  if (!context) return
  canvas.width = scene.width
  canvas.height = scene.height
  const image = context.createImageData(scene.width, scene.height)
  const pixels = image.data
  const columns = Math.ceil(scene.width / 2)
  const angularFrequency = (TAU * 2.2) / (scene.angleEnd - scene.angleStart)
  for (let y = 0; y < scene.height; y += 2) {
    for (let x = 0; x < scene.width; x += 2) {
      const distance =
        Math.hypot(x - scene.centerX, y - scene.centerY) - scene.radius
      const shade = vignette(x + 1, y + 1, scene)
      let color: readonly number[] = palette.ground
      let light = shade
      if (distance >= 0) {
        const blue = Math.exp(-distance / (14 * scene.scale))
        const pink =
          0.85 *
          Math.exp(-distance / (120 * scene.scale)) *
          Math.min(1, distance / (10 * scene.scale))
        const plum =
          0.7 *
          Math.exp(-distance / (600 * scene.scale)) *
          Math.min(1, distance / (30 * scene.scale))
        const roll = unit((y / 2) * columns + x / 2 + random(463))
        if (distance < 5 * scene.scale || roll < blue) {
          color = palette.rim
          light *=
            1 +
            0.16 *
              Math.sin(
                Math.atan2(y - scene.centerY, x - scene.centerX) *
                  angularFrequency
              )
        } else if (roll < blue + pink) color = palette.pink
        else if (roll < Math.min(1, blue + pink + plum)) color = palette.plum
      }
      for (let dy = 0; dy < 2 && y + dy < scene.height; dy++) {
        for (let dx = 0; dx < 2 && x + dx < scene.width; dx++) {
          const pixel = (y + dy) * scene.width + x + dx
          const grain = (unit(pixel + 713) - 0.5) * 24
          const offset = pixel * 4
          pixels[offset] = color[0]! * light + grain
          pixels[offset + 1] = color[1]! * light + grain
          pixels[offset + 2] = color[2]! * light + grain
          pixels[offset + 3] = 255
        }
      }
    }
  }
  for (let index = 0; index < particles.length; index += 7) {
    const depth = particles[index + 3]!
    const radius = scene.radius - depth
    const theta =
      particles[index]! -
      (particles[index + 4]! * Math.cos(particles[index + 6]!)) / radius
    const x = scene.centerX + radius * Math.cos(theta)
    const y = scene.centerY + radius * Math.sin(theta)
    const opacity = Math.max(
      0,
      Math.min(
        1,
        depth / 3,
        (particles[index + 1]! - depth) / (particles[index + 1]! * 0.25)
      )
    )
    const light = vignette(x, y, scene)
    const left = Math.ceil(x - 1.5)
    const top = Math.ceil(y - 1.5)
    for (let py = top; py < top + 2; py++) {
      if (py < 0 || py >= scene.height) continue
      for (let px = left; px < left + 2; px++) {
        if (px < 0 || px >= scene.width) continue
        const offset = (py * scene.width + px) * 4
        for (let channel = 0; channel < 3; channel++) {
          pixels[offset + channel] +=
            (palette.particle[channel]! * light - pixels[offset + channel]!) *
            opacity
        }
      }
    }
  }
  for (let y = 0; y < scene.height; y++) {
    for (let x = 0; x < scene.width; x++) {
      const value = unit((y % 512) * 512 + (x % 512) + 1789)
      if (value >= 0.045) continue
      const tone = value < 0.023 ? 0 : 255
      const offset = (y * scene.width + x) * 4
      for (let channel = 0; channel < 3; channel++) {
        pixels[offset + channel] +=
          (tone - pixels[offset + channel]!) * (10 / 255)
      }
    }
  }
  context.putImageData(image, 0, 0)
}

const fullscreenVertex = `#version 300 es
const vec2 points[3] = vec2[3](vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));
void main() { gl_Position = vec4(points[gl_VertexID], 0.0, 1.0); }
`

const sceneUniforms = `
uniform vec2 u_resolution;
uniform vec4 u_circle;
uniform vec2 u_angles;
uniform float u_time;
float vignette(vec2 point) {
  return max(0.2, 1.0 - pow(length(point - u_resolution * 0.5) / (1600.0 * u_circle.w), 3.0));
}
`

const randomSource = `
uint randomBits(uint seed) {
  uint state = seed * 747796405u + 2891336453u;
  uint mixed = ((state >> ((state >> 28u) + 4u)) ^ state) * 277803737u;
  return (mixed >> 22u) ^ mixed;
}
float randomUnit(uint seed) { return float(randomBits(seed)) / 4294967296.0; }
`

const fieldFragment = `#version 300 es
precision highp float;
precision highp int;
${sceneUniforms}
${randomSource}
out vec4 color;
void main() {
  vec2 point = vec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y);
  vec2 cell = floor(point / 2.0) * 2.0;
  vec2 radial = cell - u_circle.xy;
  float distance = length(radial) - u_circle.z;
  float shade = vignette(cell + 1.0);
  vec3 ink = vec3(10.0, 9.0, 11.0);
  float shimmer = 1.0 + 0.16 * sin(atan(radial.y, radial.x) * (6.2831853 * 2.2 / (u_angles.y - u_angles.x)) - u_time * (6.2831853 / 5.45));
  if (distance >= 0.0) {
    float blue = exp(-distance / (14.0 * u_circle.w));
    float pink = 0.85 * exp(-distance / (120.0 * u_circle.w)) * min(1.0, distance / (10.0 * u_circle.w));
    float plum = 0.7 * exp(-distance / (600.0 * u_circle.w)) * min(1.0, distance / (30.0 * u_circle.w));
    uint cellIndex = uint(cell.y / 2.0) * uint(ceil(u_resolution.x / 2.0)) + uint(cell.x / 2.0);
    uint bucket = randomBits(cellIndex + 2719u) % 90u;
    uint generation = uint(floor((u_time + float(bucket) / 60.0) / 1.5));
    float roll = randomUnit(cellIndex + randomBits(generation + 463u));
    if (distance < 5.0 * u_circle.w || roll < blue) ink = vec3(137.0, 160.0, 255.0) * shimmer;
    else if (roll < blue + pink) ink = vec3(247.0, 121.0, 203.0);
    else if (roll < min(1.0, blue + pink + plum)) ink = vec3(96.0, 11.0, 47.0);
  }
  uint pixel = uint(floor(point.y)) * uint(u_resolution.x) + uint(floor(point.x));
  float grain = (randomUnit(pixel + 713u) - 0.5) * 24.0;
  color = vec4(max(vec3(0.0), ink * shade + grain) / 255.0, 1.0);
}
`

const particleVertex = `#version 300 es
precision highp float;
${sceneUniforms}
layout(location = 0) in vec4 a_orbit;
layout(location = 1) in vec3 a_wander;
out float opacity;
out float illumination;
const vec2 corners[6] = vec2[6](vec2(-1.0, -1.0), vec2(1.0, -1.0), vec2(-1.0, 1.0), vec2(-1.0, 1.0), vec2(1.0, -1.0), vec2(1.0, 1.0));
void main() {
  float depth = mod(u_time * a_orbit.z + a_orbit.w, a_orbit.y);
  float radius = u_circle.z - depth;
  float theta = a_orbit.x - a_wander.x * cos(u_time * a_wander.y + a_wander.z) / max(radius, 1.0);
  vec2 center = u_circle.xy + radius * vec2(cos(theta), sin(theta));
  vec2 point = center + corners[gl_VertexID];
  gl_Position = vec4(point.x / u_resolution.x * 2.0 - 1.0, 1.0 - point.y / u_resolution.y * 2.0, 0.0, 1.0);
  opacity = clamp(min(depth / 3.0, (a_orbit.y - depth) / (a_orbit.y * 0.25)), 0.0, 1.0);
  illumination = vignette(center);
}
`

const particleFragment = `#version 300 es
precision highp float;
in float opacity;
in float illumination;
out vec4 color;
void main() { color = vec4(vec3(251.0, 250.0, 252.0) / 255.0 * illumination, opacity); }
`

const grainFragment = `#version 300 es
precision highp float;
precision highp int;
uniform vec2 u_resolution;
${randomSource}
out vec4 color;
void main() {
  uvec2 point = uvec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y) % 512u;
  float grain = randomUnit(point.y * 512u + point.x + 1789u);
  color = grain < 0.045 ? vec4(vec3(grain < 0.023 ? 0.0 : 1.0), 10.0 / 255.0) : vec4(0.0);
}
`

type Pass = {
  program: WebGLProgram
  resolution: WebGLUniformLocation | null
  circle: WebGLUniformLocation | null
  angles: WebGLUniformLocation | null
  time: WebGLUniformLocation | null
}

function createPass(
  gl: WebGL2RenderingContext,
  vertex: string,
  fragment: string
): Pass {
  const program = gl.createProgram()
  if (!program) throw new Error("Program unavailable")
  const shaders: WebGLShader[] = []
  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, vertex],
      [gl.FRAGMENT_SHADER, fragment],
    ] as const) {
      const shader = gl.createShader(type)
      if (!shader) throw new Error("Shader unavailable")
      shaders.push(shader)
      gl.shaderSource(shader, source)
      gl.compileShader(shader)
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
        throw new Error("Shader compilation failed")
      gl.attachShader(program, shader)
    }
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS))
      throw new Error("Shader linking failed")
    return {
      program,
      resolution: gl.getUniformLocation(program, "u_resolution"),
      circle: gl.getUniformLocation(program, "u_circle"),
      angles: gl.getUniformLocation(program, "u_angles"),
      time: gl.getUniformLocation(program, "u_time"),
    }
  } catch (error) {
    gl.deleteProgram(program)
    throw error
  } finally {
    for (const shader of shaders) gl.deleteShader(shader)
  }
}

function createRenderer(gl: WebGL2RenderingContext) {
  const passes: Pass[] = []
  const fullscreen = gl.createVertexArray()
  const particles = gl.createVertexArray()
  const buffer = gl.createBuffer()
  const dispose = () => {
    for (const pass of passes) gl.deleteProgram(pass.program)
    if (fullscreen) gl.deleteVertexArray(fullscreen)
    if (particles) gl.deleteVertexArray(particles)
    if (buffer) gl.deleteBuffer(buffer)
  }
  try {
    if (!fullscreen || !particles || !buffer)
      throw new Error("Geometry unavailable")
    passes.push(createPass(gl, fullscreenVertex, fieldFragment))
    passes.push(createPass(gl, particleVertex, particleFragment))
    passes.push(createPass(gl, fullscreenVertex, grainFragment))
    gl.bindVertexArray(particles)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.enableVertexAttribArray(0)
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 28, 0)
    gl.vertexAttribDivisor(0, 1)
    gl.enableVertexAttribArray(1)
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 28, 16)
    gl.vertexAttribDivisor(1, 1)
    gl.bindVertexArray(null)
    let scene = arcGeometry(1, 1)
    const activate = (pass: Pass, time: number) => {
      gl.useProgram(pass.program)
      gl.uniform2f(pass.resolution, scene.width, scene.height)
      gl.uniform4f(
        pass.circle,
        scene.centerX,
        scene.centerY,
        scene.radius,
        scene.scale
      )
      gl.uniform2f(pass.angles, scene.angleStart, scene.angleEnd)
      gl.uniform1f(pass.time, time)
    }
    return {
      resize(next: ArcGeometry, data: Float32Array) {
        scene = next
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
      },
      draw(time: number) {
        gl.viewport(0, 0, scene.width, scene.height)
        gl.disable(gl.BLEND)
        gl.bindVertexArray(fullscreen)
        activate(passes[0]!, time)
        gl.drawArrays(gl.TRIANGLES, 0, 3)
        gl.enable(gl.BLEND)
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)
        gl.bindVertexArray(particles)
        activate(passes[1]!, time)
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, scene.particleCount)
        gl.bindVertexArray(fullscreen)
        activate(passes[2]!, time)
        gl.drawArrays(gl.TRIANGLES, 0, 3)
        gl.disable(gl.BLEND)
        gl.bindVertexArray(null)
      },
      dispose,
    }
  } catch {
    dispose()
    return null
  }
}

export type LandscapeStatus =
  "loading" | "playing" | "paused" | "reduced" | "unavailable"

export function mountLandscape(
  canvas: HTMLCanvasElement,
  poster: HTMLCanvasElement,
  onStatus: (status: LandscapeStatus) => void
) {
  let gl: WebGL2RenderingContext | null = null
  try {
    gl = canvas.getContext("webgl2", {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      powerPreference: "low-power",
    })
  } catch {
    // The static poster stays visible when the browser blocks WebGL.
  }
  let renderer = gl ? createRenderer(gl) : null
  let media: MediaQueryList | null = null
  let resizeObserver: ResizeObserver | null = null
  let intersectionObserver: IntersectionObserver | null = null
  let scene: ArcGeometry | null = null
  let seeds: Float32Array = new Float32Array()
  let paused = false
  let disposed = false
  let inView = true
  let resizeFrame: number | null = null
  let lastStatus: LandscapeStatus = "loading"
  const loop = createLandscapeLoop((time) => {
    if (!renderer) return
    renderer.draw(time)
    if (canvas.dataset.ready !== "true") canvas.dataset.ready = "true"
  })
  const synchronize = () => {
    if (disposed) return
    const status = !renderer
      ? "unavailable"
      : media?.matches
        ? "reduced"
        : paused
          ? "paused"
          : "playing"
    if (status !== lastStatus) {
      lastStatus = status
      onStatus(status)
    }
    if (renderer && !document.hidden && inView) {
      if (status === "playing") loop.start()
      else {
        loop.stop()
        loop.drawStill()
      }
    } else loop.stop()
  }
  const resize = () => {
    if (disposed) return
    const rect = canvas.getBoundingClientRect()
    const width = Math.max(1, rect.width)
    const height = Math.max(1, rect.height)
    const ratio = Math.min(
      Math.max(1, window.devicePixelRatio || 1),
      Math.sqrt(2200000 / (width * height))
    )
    const renderWidth = Math.max(1, Math.round(width * ratio))
    const renderHeight = Math.max(1, Math.round(height * ratio))
    if (
      !scene ||
      scene.width !== renderWidth ||
      scene.height !== renderHeight
    ) {
      scene = arcGeometry(renderWidth, renderHeight)
      seeds = particleData(scene)
      canvas.width = renderWidth
      canvas.height = renderHeight
      drawPoster(poster, scene, seeds)
    }
    renderer?.resize(scene, seeds)
    if (!document.hidden && inView) loop.drawStill()
  }
  const onContextLost = (event: Event) => {
    event.preventDefault()
    loop.stop()
    renderer = null
    canvas.dataset.ready = "false"
    synchronize()
  }
  const requestResize = () => {
    if (disposed || resizeFrame !== null) return
    resizeFrame = window.requestAnimationFrame(() => {
      resizeFrame = null
      resize()
    })
  }
  const onContextRestored = () => {
    if (disposed || !gl) return
    renderer = createRenderer(gl)
    resize()
    synchronize()
  }
  const dispose = () => {
    if (disposed) return
    disposed = true
    loop.stop()
    if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame)
    resizeFrame = null
    resizeObserver?.disconnect()
    intersectionObserver?.disconnect()
    window.removeEventListener("resize", requestResize)
    document.removeEventListener("visibilitychange", synchronize)
    media?.removeEventListener("change", synchronize)
    canvas.removeEventListener("webglcontextlost", onContextLost)
    canvas.removeEventListener("webglcontextrestored", onContextRestored)
    renderer?.dispose()
    renderer = null
    canvas.dataset.ready = "false"
  }
  try {
    resize()
    media = window.matchMedia("(prefers-reduced-motion: reduce)")
    resizeObserver = new ResizeObserver(requestResize)
    intersectionObserver = new IntersectionObserver(([entry]) => {
      inView = entry?.isIntersecting ?? false
      synchronize()
    })
    resizeObserver.observe(canvas)
    intersectionObserver.observe(canvas)
    window.addEventListener("resize", requestResize)
    document.addEventListener("visibilitychange", synchronize)
    media.addEventListener("change", synchronize)
    canvas.addEventListener("webglcontextlost", onContextLost)
    canvas.addEventListener("webglcontextrestored", onContextRestored)
    synchronize()
  } catch {
    dispose()
    onStatus("unavailable")
    return { togglePaused() {}, dispose() {} }
  }
  return {
    togglePaused() {
      paused = !paused
      synchronize()
    },
    dispose,
  }
}
