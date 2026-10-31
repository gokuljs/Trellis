import { createLandscapeLoop } from "./dither-landscape-loop"

const vertexSource = `
attribute vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`

const fragmentSource = `
precision highp float;
uniform vec2 u_resolution;
uniform vec2 u_css_resolution;
uniform float u_time;

float hash(vec2 point) {
  vec3 p = fract(vec3(point.xyx) * 0.1031);
  p += dot(p, p.yzx + 33.33);
  return fract((p.x + p.y) * p.z);
}

float noise(vec2 point) {
  vec2 cell = floor(point);
  vec2 p = fract(point);
  p = p * p * (3.0 - 2.0 * p);
  return mix(
    mix(hash(cell), hash(cell + vec2(1.0, 0.0)), p.x),
    mix(hash(cell + vec2(0.0, 1.0)), hash(cell + vec2(1.0)), p.x),
    p.y
  );
}

float terrain(vec2 point) {
  float height = 0.0;
  float weight = 0.5;
  for (int i = 0; i < 4; i++) {
    height += noise(point) * weight;
    point = mat2(1.6, -1.2, 1.2, 1.6) * point + 4.3;
    weight *= 0.5;
  }
  return height;
}

float bayer2(vec2 point) {
  return mod(2.0 * point.x + 3.0 * point.y, 4.0);
}

float bayer4(vec2 point) {
  vec2 cell = mod(point, 4.0);
  return (4.0 * bayer2(mod(cell, 2.0)) + bayer2(floor(cell / 2.0)) + 0.5) / 16.0;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution;
  vec2 pixel = floor(uv * u_css_resolution / 2.0);
  float time = u_time * 0.12;
  vec2 p = (uv - vec2(1.13, -0.13)) * vec2(1.04, 1.0);
  float angle = atan(p.y, p.x);
  float radius = 0.91 + sin(angle * 4.0 + time) * 0.024
    + sin(angle * 9.0 - time * 0.7) * 0.012;
  vec2 drift = vec2(sin(time * 0.7), cos(time * 0.6)) * 0.12;
  float field = terrain(p * 3.2 + drift);
  float distance = length(p) - radius + (field - 0.5) * 0.045;
  float surface = 1.0 - smoothstep(-0.003, 0.009, distance);
  float rim = exp(-abs(distance) * 38.0);
  float folds = terrain(vec2(angle * 3.0, length(p) * 7.0) + drift * 0.5);
  float shade = 0.12 + field * 0.34 + folds * 0.14;
  float coverage = clamp(surface * shade + rim * 0.61, 0.0, 0.94);
  float threshold = bayer4(pixel);
  float dots = step(threshold, coverage + (hash(pixel) - 0.5) * 0.04);

  float leftFade = smoothstep(0.43, 0.82, uv.x);
  float portrait = 1.0 - smoothstep(0.8, 1.2, u_css_resolution.x / u_css_resolution.y);
  float mobileFade = 1.0 - smoothstep(0.12, 0.42, uv.y);
  float fade = mix(leftFade, mobileFade * smoothstep(0.0, 0.8, uv.x), portrait);
  vec3 olive = vec3(0.35, 0.39, 0.20);
  vec3 ivory = vec3(0.92, 0.94, 0.70);
  vec3 ink = vec3(0.025, 0.029, 0.015) * surface;
  vec3 color = ink + dots * mix(olive, ivory, rim) * (0.55 + field * 0.5);
  float dust = step(0.998, hash(pixel + 91.0)) * 0.2;
  color += dust * vec3(0.66, 0.68, 0.47) * (1.0 - surface);
  gl_FragColor = vec4(color * fade, 1.0);
}
`

export type LandscapeStatus =
  "loading" | "playing" | "paused" | "reduced" | "unavailable"

function createRenderer(gl: WebGLRenderingContext) {
  const shaders: WebGLShader[] = []
  const program = gl.createProgram()
  const buffer = gl.createBuffer()
  if (!program || !buffer) {
    if (program) gl.deleteProgram(program)
    if (buffer) gl.deleteBuffer(buffer)
    return null
  }

  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, vertexSource],
      [gl.FRAGMENT_SHADER, fragmentSource],
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
    const position = gl.getAttribLocation(program, "a_position")
    const resolution = gl.getUniformLocation(program, "u_resolution")
    const cssResolution = gl.getUniformLocation(program, "u_css_resolution")
    const time = gl.getUniformLocation(program, "u_time")
    if (position < 0 || !resolution || !cssResolution || !time)
      throw new Error("Shader inputs unavailable")
    gl.useProgram(program)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]),
      gl.STATIC_DRAW
    )
    gl.enableVertexAttribArray(position)
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0)
    return {
      draw(elapsed: number, width: number, height: number) {
        gl.viewport(0, 0, gl.canvas.width, gl.canvas.height)
        gl.uniform2f(resolution, gl.canvas.width, gl.canvas.height)
        gl.uniform2f(cssResolution, width, height)
        gl.uniform1f(time, elapsed)
        gl.drawArrays(gl.TRIANGLES, 0, 3)
      },
      dispose() {
        gl.deleteBuffer(buffer)
        gl.deleteProgram(program)
      },
    }
  } catch {
    gl.deleteBuffer(buffer)
    gl.deleteProgram(program)
    return null
  } finally {
    for (const shader of shaders) gl.deleteShader(shader)
  }
}

export function mountLandscape(
  canvas: HTMLCanvasElement,
  onStatus: (status: LandscapeStatus) => void
) {
  let gl: WebGLRenderingContext | null = null
  try {
    gl = canvas.getContext("webgl", {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      powerPreference: "low-power",
    })
  } catch {
    // The CSS landscape remains available when the browser blocks WebGL.
  }
  let renderer = gl ? createRenderer(gl) : null
  if (!gl || !renderer) {
    onStatus("unavailable")
    return { togglePaused() {}, dispose() {} }
  }

  const context = gl
  let media: MediaQueryList | null = null
  let resizeObserver: ResizeObserver | null = null
  let intersectionObserver: IntersectionObserver | null = null
  let paused = false
  let disposed = false
  let inView = true
  let width = 1
  let height = 1
  let lastStatus: LandscapeStatus = "loading"
  const loop = createLandscapeLoop((time) => {
    if (!renderer) return
    renderer.draw(time, width, height)
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
    width = Math.max(1, rect.width)
    height = Math.max(1, rect.height)
    const scale = Math.min(
      window.devicePixelRatio || 1,
      1.5,
      Math.sqrt(900000 / (width * height))
    )
    canvas.width = Math.max(1, Math.round(width * scale))
    canvas.height = Math.max(1, Math.round(height * scale))
    if (!document.hidden && inView) loop.drawStill()
  }

  const onContextLost = (event: Event) => {
    event.preventDefault()
    loop.stop()
    renderer = null
    canvas.dataset.ready = "false"
    synchronize()
  }
  const onContextRestored = () => {
    if (disposed) return
    renderer = createRenderer(context)
    resize()
    synchronize()
  }

  const dispose = () => {
    if (disposed) return
    disposed = true
    loop.stop()
    resizeObserver?.disconnect()
    intersectionObserver?.disconnect()
    window.removeEventListener("resize", resize)
    document.removeEventListener("visibilitychange", synchronize)
    media?.removeEventListener("change", synchronize)
    canvas.removeEventListener("webglcontextlost", onContextLost)
    canvas.removeEventListener("webglcontextrestored", onContextRestored)
    renderer?.dispose()
    renderer = null
    canvas.dataset.ready = "false"
  }

  try {
    media = window.matchMedia("(prefers-reduced-motion: reduce)")
    resizeObserver = new ResizeObserver(resize)
    intersectionObserver = new IntersectionObserver(([entry]) => {
      inView = entry?.isIntersecting ?? false
      synchronize()
    })
    resizeObserver.observe(canvas)
    intersectionObserver.observe(canvas)
    window.addEventListener("resize", resize)
    document.addEventListener("visibilitychange", synchronize)
    media.addEventListener("change", synchronize)
    canvas.addEventListener("webglcontextlost", onContextLost)
    canvas.addEventListener("webglcontextrestored", onContextRestored)
    resize()
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
