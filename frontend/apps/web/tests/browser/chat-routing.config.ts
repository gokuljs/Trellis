import path from "node:path"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vite"

export default defineConfig({
  root: path.resolve(import.meta.dirname, "../.."),
  envDir: false,
  plugins: [
    {
      name: "chat-routing-fixture",
      configureServer(server) {
        server.middlewares.use((request, _response, next) => {
          const pathname = new URL(request.url ?? "/", "http://fixture.test")
            .pathname
          if (
            pathname === "/" ||
            pathname === "/login" ||
            pathname === "/signup" ||
            pathname.startsWith("/chats/") ||
            pathname === "/unknown"
          )
            request.url = "/tests/browser/chat-routing.html"
          next()
        })
      },
    },
    react(),
    tailwindcss(),
  ],
  server: { host: "127.0.0.1", port: 3002, strictPort: true },
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "../../src"),
      "@trellis/ui/globals.css": path.resolve(
        import.meta.dirname,
        "../../../../packages/ui/src/styles/globals.css"
      ),
      "@trellis/ui": path.resolve(
        import.meta.dirname,
        "../../../../packages/ui/src"
      ),
    },
  },
})
