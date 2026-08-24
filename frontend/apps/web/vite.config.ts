import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    strictPort: true,
  },
  preview: {
    port: 3000,
    strictPort: true,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@trellis/ui/globals.css": path.resolve(__dirname, "../../packages/ui/src/styles/globals.css"),
      "@trellis/ui": path.resolve(__dirname, "../../packages/ui/src"),
    },
  },
})
