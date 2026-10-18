import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { sentryVitePlugin } from "@sentry/vite-plugin"
import { defineConfig, loadEnv } from "vite"
import { sentryBuildConfig } from "./config/sentry-build.ts"

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  const envDir = path.resolve(import.meta.dirname, "../..")
  const environment = { ...loadEnv(mode, envDir, ""), ...process.env }
  const sentry = sentryBuildConfig(environment, import.meta.dirname, command)
  return {
    envDir,
    plugins: [
      react(),
      tailwindcss(),
      ...(sentry.pluginOptions ? sentryVitePlugin(sentry.pluginOptions) : []),
    ],
    build: { outDir: sentry.outDir, sourcemap: sentry.sourcemap },
    server: {
      port: 3000,
      strictPort: true,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          ws: true,
        },
      },
    },
    preview: {
      port: 3000,
      strictPort: true,
    },
    resolve: {
      alias: {
        "@": path.resolve(import.meta.dirname, "./src"),
        "@trellis/ui/globals.css": path.resolve(
          import.meta.dirname,
          "../../packages/ui/src/styles/globals.css"
        ),
        "@trellis/ui": path.resolve(
          import.meta.dirname,
          "../../packages/ui/src"
        ),
      },
    },
  }
})
