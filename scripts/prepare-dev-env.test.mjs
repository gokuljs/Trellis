import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import test from "node:test"

test("missing frontend dependencies explain how to install them", (t) => {
  const repository = mkdtempSync(path.join(tmpdir(), "trellis-dev-env-"))
  t.after(() => rmSync(repository, { recursive: true, force: true }))

  mkdirSync(path.join(repository, "scripts"))
  mkdirSync(path.join(repository, "frontend/apps/web"), { recursive: true })
  copyFileSync(
    new URL("./prepare-dev-env.mjs", import.meta.url),
    path.join(repository, "scripts/prepare-dev-env.mjs")
  )
  writeFileSync(path.join(repository, "frontend/apps/web/package.json"), "{}")
  writeFileSync(
    path.join(repository, "frontend/.env"),
    "VITE_SUPABASE_URL=https://fixture.supabase.co\nVITE_SUPABASE_PUBLISHABLE_KEY=fixture-secret\n"
  )

  const environment = { ...process.env }
  delete environment.VITE_SUPABASE_URL
  delete environment.VITE_SUPABASE_PUBLISHABLE_KEY
  delete environment.NODE_PATH
  const result = spawnSync(
    process.execPath,
    [path.join(repository, "scripts/prepare-dev-env.mjs"), "--frontend-only"],
    { cwd: repository, env: environment, encoding: "utf8", timeout: 10_000 }
  )

  assert.ifError(result.error)
  assert.equal(result.status, 1)
  assert.match(result.stderr, /cd frontend && bun install --frozen-lockfile/)
  assert.doesNotMatch(result.stdout + result.stderr, /fixture-secret/)
})
