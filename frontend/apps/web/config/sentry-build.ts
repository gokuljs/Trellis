import path from "node:path"
import type { SentryVitePluginOptions } from "@sentry/vite-plugin"

type SentryBuildEnvironment = Partial<
  Record<
    | "SENTRY_UPLOAD_SOURCEMAPS"
    | "SENTRY_ORG"
    | "SENTRY_PROJECT"
    | "SENTRY_AUTH_TOKEN"
    | "VITE_SENTRY_RELEASE"
    | "NODE_ENV",
    string
  >
>

export function sentryBuildConfig(
  env: SentryBuildEnvironment,
  webRoot: string,
  command: "build" | "serve"
): {
  outDir: string
  sourcemap: false | "hidden"
  pluginOptions?: SentryVitePluginOptions
} {
  const outDir = path.resolve(webRoot, "dist")
  const flag = env.SENTRY_UPLOAD_SOURCEMAPS?.trim()
  if (command === "serve" || !flag || flag === "false")
    return { outDir, sourcemap: false }
  if (flag !== "true")
    throw new Error("SENTRY_UPLOAD_SOURCEMAPS must be true or false.")
  if (env.NODE_ENV === "development") {
    throw new Error("Sentry source-map uploads require a production NODE_ENV.")
  }
  const required = (name: keyof SentryBuildEnvironment) => {
    const value = env[name]?.trim()
    if (!value)
      throw new Error(`${name} is required for Sentry source-map uploads.`)
    return value
  }
  const org = required("SENTRY_ORG")
  const project = required("SENTRY_PROJECT")
  const authToken = required("SENTRY_AUTH_TOKEN")
  const release = required("VITE_SENTRY_RELEASE")
  if (
    !/^[\w.@+-]{1,200}$/.test(release) ||
    [".", "..", "latest"].includes(release)
  ) {
    throw new Error(
      "VITE_SENTRY_RELEASE must be a valid release identifier, such as the Git commit SHA."
    )
  }
  if (!/^[a-z0-9_-]+$/i.test(org) || !/^[a-z0-9_-]+$/i.test(project)) {
    throw new Error("SENTRY_ORG and SENTRY_PROJECT must be Sentry slugs.")
  }
  return {
    outDir,
    sourcemap: "hidden",
    pluginOptions: {
      org,
      project,
      authToken,
      telemetry: false,
      release: {
        name: release,
        inject: false,
        create: false,
        finalize: false,
        setCommits: false,
        deploy: false,
      },
      sourcemaps: {
        assets: [path.join(outDir, "**/*.{js,mjs,map}")],
        filesToDeleteAfterUpload: [path.join(outDir, "**/*.map")],
      },
      errorHandler(error) {
        throw error
      },
    },
  }
}
