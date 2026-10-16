import assert from "node:assert/strict"
import path from "node:path"
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { afterEach, test } from "bun:test"

import { sentryBuildConfig } from "../config/sentry-build"

const webRoot = path.resolve(import.meta.dir, "..")
const token = "private-build-only-sentry-token"
const environment = {
  SENTRY_UPLOAD_SOURCEMAPS: "true",
  SENTRY_ORG: "trellis-test",
  SENTRY_PROJECT: "frontend-test",
  SENTRY_AUTH_TOKEN: token,
  VITE_SENTRY_RELEASE: "web@1234567",
}
const temporary: string[] = []
afterEach(async () => {
  for (const directory of temporary.splice(0))
    await rm(directory, { recursive: true, force: true })
})

test("ordinary builds and dev servers do not enable source maps or the Sentry plugin", () => {
  const ordinary = sentryBuildConfig({}, webRoot, "build")
  assert.equal(ordinary.sourcemap, false)
  assert.equal(ordinary.pluginOptions, undefined)
  assert.equal(
    sentryBuildConfig(environment, webRoot, "serve").pluginOptions,
    undefined
  )
  assert.equal(
    sentryBuildConfig({ SENTRY_UPLOAD_SOURCEMAPS: "false" }, webRoot, "build")
      .pluginOptions,
    undefined
  )
})

test("upload builds require explicit valid configuration without exposing the build token", () => {
  for (const key of [
    "SENTRY_ORG",
    "SENTRY_PROJECT",
    "SENTRY_AUTH_TOKEN",
    "VITE_SENTRY_RELEASE",
  ] as const) {
    assert.throws(
      () => sentryBuildConfig({ ...environment, [key]: "" }, webRoot, "build"),
      (error) =>
        error instanceof Error &&
        error.message.includes(key) &&
        !error.message.includes(token)
    )
  }
  for (const release of [
    "bad/release",
    "..",
    "latest",
    "line\nbreak",
    "x".repeat(201),
  ]) {
    assert.throws(
      () =>
        sentryBuildConfig(
          { ...environment, VITE_SENTRY_RELEASE: release },
          webRoot,
          "build"
        ),
      /VITE_SENTRY_RELEASE/
    )
  }
  assert.throws(
    () =>
      sentryBuildConfig({ SENTRY_UPLOAD_SOURCEMAPS: "yes" }, webRoot, "build"),
    /SENTRY_UPLOAD_SOURCEMAPS/
  )
  assert.throws(
    () =>
      sentryBuildConfig(
        { ...environment, NODE_ENV: "development" },
        webRoot,
        "build"
      ),
    /NODE_ENV/
  )
})

test("upload options use one release and narrowly scoped maps with fatal upload errors", () => {
  const result = sentryBuildConfig(environment, webRoot, "build")
  assert.equal(result.sourcemap, "hidden")
  assert.equal(result.outDir, path.join(webRoot, "dist"))
  const options = result.pluginOptions!
  assert.equal(options.authToken, token)
  assert.equal(options.telemetry, false)
  assert.equal(options.release?.name, environment.VITE_SENTRY_RELEASE)
  assert.equal(options.release?.setCommits, false)
  assert.equal(options.release?.deploy, false)
  assert.deepEqual(options.sourcemaps?.assets, [
    path.join(webRoot, "dist/**/*.{js,mjs,map}"),
  ])
  assert.deepEqual(options.sourcemaps?.filesToDeleteAfterUpload, [
    path.join(webRoot, "dist/**/*.map"),
  ])
  const failure = new Error("upload failed")
  assert.throws(
    () => options.errorHandler!(failure),
    (error) => error === failure
  )
})

test("Vite keeps Sentry last and uses process configuration from the frontend environment directory", async () => {
  async function inspect(upload: string) {
    const script = `
      import { loadConfigFromFile } from 'vite';
      const result = await loadConfigFromFile({ command: 'build', mode: 'production' }, './vite.config.ts');
      const config = result.config;
      const plugins = (await Promise.all(config.plugins)).flat(Infinity).filter(Boolean).map(plugin => plugin.name);
      console.log(JSON.stringify({ envDir: config.envDir, sourcemap: config.build.sourcemap, plugins }));
    `
    const child = Bun.spawn(["node", "--input-type=module", "--eval", script], {
      cwd: webRoot,
      env: { ...process.env, ...environment, SENTRY_UPLOAD_SOURCEMAPS: upload },
      stdout: "pipe",
      stderr: "pipe",
    })
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ])
    assert.equal(exit, 0, stderr)
    return JSON.parse(stdout.trim()) as {
      envDir: string
      sourcemap: false | "hidden"
      plugins: string[]
    }
  }
  const configured = await inspect("true")
  assert.equal(configured.envDir, path.resolve(webRoot, "../.."))
  assert.equal(configured.sourcemap, "hidden")
  assert.ok(configured.plugins.some((name) => name.includes("react")))
  assert.ok(configured.plugins.some((name) => name.includes("tailwind")))
  assert.ok(
    configured.plugins.at(-1)?.includes("sentry"),
    JSON.stringify(configured.plugins)
  )
  const ordinary = await inspect("false")
  assert.equal(ordinary.sourcemap, false)
  assert.equal(
    ordinary.plugins.some((name) => name.includes("sentry")),
    false
  )
})

test("the explicit upload command fails before building when required settings are absent", async () => {
  const child = Bun.spawn([process.execPath, "run", "build:sentry"], {
    cwd: path.resolve(webRoot, "../.."),
    env: {
      ...process.env,
      SENTRY_ORG: "",
      SENTRY_PROJECT: "",
      SENTRY_AUTH_TOKEN: "",
      VITE_SENTRY_RELEASE: "",
      TURBO_TELEMETRY_DISABLED: "1",
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  const [exit, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ])
  assert.notEqual(exit, 0)
  assert.ok(
    (stdout + stderr).includes("SENTRY_ORG is required"),
    stdout + stderr
  )
}, 20_000)

test("an ordinary isolated production build emits no maps or build-only token", async () => {
  const output = await mkdtemp(path.join(tmpdir(), "trellis-sentry-build-"))
  temporary.push(output)
  const child = Bun.spawn(
    [
      process.execPath,
      "x",
      "vite",
      "build",
      "--outDir",
      output,
      "--emptyOutDir",
    ],
    {
      cwd: webRoot,
      env: {
        ...process.env,
        SENTRY_UPLOAD_SOURCEMAPS: "false",
        SENTRY_AUTH_TOKEN: token,
      },
      stdout: "pipe",
      stderr: "pipe",
    }
  )
  const [exit, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ])
  assert.equal(exit, 0, stdout + stderr)
  const files = await readdir(output, { recursive: true })
  assert.ok(files.some((file) => file.endsWith(".js")))
  assert.equal(
    files.some((file) => file.endsWith(".map")),
    false
  )
  for (const file of files.filter((file) => /\.(?:js|html|css)$/.test(file))) {
    const contents = await readFile(path.join(output, file), "utf8")
    assert.equal(contents.includes(token), false, file)
    assert.equal(contents.includes("sourceMappingURL="), false, file)
  }
}, 20_000)
