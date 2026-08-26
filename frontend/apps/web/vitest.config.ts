import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

export default defineConfig({
  root: import.meta.dirname,
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
      "@trellis/ui/globals.css": path.resolve(
        import.meta.dirname,
        "../../packages/ui/src/styles/globals.css"
      ),
      "@trellis/ui": path.resolve(import.meta.dirname, "../../packages/ui/src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
})
