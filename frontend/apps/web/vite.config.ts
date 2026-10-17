import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, loadEnv } from "vite"
import { parseBackendTargets } from "./src/lib/backend-targets.ts"

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  const envDir = path.resolve(import.meta.dirname, "../..")
  parseBackendTargets(loadEnv(mode, envDir, "VITE_"), command === "build")
  return {
    envDir: path.resolve(import.meta.dirname, "../.."),
    plugins: [react(), tailwindcss()],
    server: {
      port: 3000,
      strictPort: true,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          ws: true,
        },
        "/ready": {
          target: "http://127.0.0.1:8000",
        },
        "/health": {
          target: "http://127.0.0.1:8000",
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
