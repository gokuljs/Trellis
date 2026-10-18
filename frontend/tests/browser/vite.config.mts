import path from "node:path"
import { defineConfig, mergeConfig } from "vite"

import applicationConfig from "../../apps/web/vite.config.ts"

const config = mergeConfig(applicationConfig, {
  root: path.resolve(import.meta.dirname, "../../apps/web"),
  server: {
    host: "127.0.0.1",
    port: 3039,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8123",
        ws: true,
      },
    },
  },
})

config.resolve = {
  ...config.resolve,
  alias: [
    {
      find: "@/lib/supabase-client",
      replacement: path.resolve(import.meta.dirname, "auth-client.ts"),
    },
    ...Object.entries(applicationConfig.resolve?.alias ?? {}).map(
      ([find, replacement]) => ({
        find,
        replacement,
      })
    ),
  ],
}

export default defineConfig(config)
