import { execFileSync } from "node:child_process"
import {
  closeSync,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import path from "node:path"
import { createRequire } from "node:module"
import { fileURLToPath, pathToFileURL } from "node:url"
import { parseEnv } from "node:util"

const repositoryRoot = fileURLToPath(new URL("..", import.meta.url))
const components = [
  {
    name: "frontend",
    files: [".env", ".env.local", ".env.development", ".env.development.local"],
    required: ["VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"],
  },
  {
    name: "backend",
    files: [".env"],
    required: ["TRELLIS_SUPABASE_URL", "TRELLIS_SUPABASE_PUBLISHABLE_KEY"],
  },
]

class SetupError extends Error {}

function exists(file) {
  try {
    lstatSync(file)
    return true
  } catch (error) {
    if (error.code === "ENOENT") return false
    throw error
  }
}

function uppercaseKeys(environment) {
  return Object.fromEntries(
    Object.entries(environment).map(([key, value]) => [
      key.toUpperCase(),
      value,
    ])
  )
}

function processEnvironment(component) {
  return component.name === "backend" ? uppercaseKeys(process.env) : process.env
}

function primaryCheckout() {
  try {
    const output = execFileSync(
      "git",
      ["worktree", "list", "--porcelain", "-z"],
      {
        cwd: repositoryRoot,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }
    )
    return output
      .split("\0")
      .find((entry) => entry.startsWith("worktree "))
      ?.slice(9)
  } catch {
    return undefined
  }
}

function inheritConfiguration(component, primary) {
  const local = path.join(repositoryRoot, component.name)
  const supplied = processEnvironment(component)
  if (
    component.required.every((key) => supplied[key]?.trim()) ||
    component.files.some((name) => exists(path.join(local, name))) ||
    !primary ||
    path.resolve(primary) === path.resolve(repositoryRoot)
  ) {
    return
  }

  let copied = false
  for (const name of component.files) {
    const source = path.join(primary, component.name, name)
    if (!exists(source)) continue
    const destination = path.join(local, name)
    let descriptor
    let created
    try {
      const content = readFileSync(source)
      descriptor = openSync(destination, "wx", 0o600)
      created = fstatSync(descriptor)
      writeFileSync(descriptor, content)
      closeSync(descriptor)
      descriptor = undefined
      copied = true
    } catch (error) {
      if (descriptor !== undefined) {
        try {
          closeSync(descriptor)
        } catch {}
      }
      if (created) {
        try {
          const current = lstatSync(destination)
          if (current.dev === created.dev && current.ino === created.ino)
            unlinkSync(destination)
        } catch {}
      }
      if (error.code !== "EEXIST") {
        throw new SetupError(`Unable to prepare ${component.name}/${name}.`)
      }
    }
  }
  if (copied) {
    console.log(
      `Copied ${component.name} local configuration from the primary checkout.`
    )
  }
}

async function effectiveEnvironment(component) {
  const directory = path.join(repositoryRoot, component.name)
  const supplied = processEnvironment(component)
  if (component.required.every((key) => supplied[key]?.trim())) return supplied
  if (!component.files.some((name) => exists(path.join(directory, name))))
    return supplied

  try {
    if (component.name === "frontend") {
      const require = createRequire(
        path.join(directory, "apps/web/package.json")
      )
      const { loadEnv } = await import(
        pathToFileURL(require.resolve("vite")).href
      )
      return { ...loadEnv("development", directory, "VITE_"), ...supplied }
    }
    return {
      ...uppercaseKeys(
        parseEnv(readFileSync(path.join(directory, ".env"), "utf8"))
      ),
      ...supplied,
    }
  } catch {
    throw new SetupError(
      `Unable to load ${component.name} development configuration. Check its environment files and installed dependencies.`
    )
  }
}

try {
  const arguments_ = process.argv.slice(2)
  if (
    arguments_.length > 1 ||
    (arguments_.length === 1 && arguments_[0] !== "--frontend-only")
  ) {
    throw new SetupError(
      "Usage: node scripts/prepare-dev-env.mjs [--frontend-only]"
    )
  }
  const selected = arguments_.length === 1 ? components.slice(0, 1) : components
  const primary = primaryCheckout()
  for (const component of selected) {
    inheritConfiguration(component, primary)
    const environment = await effectiveEnvironment(component)
    if (component.required.some((key) => !environment[key]?.trim())) {
      throw new SetupError(
        `Missing ${component.name} Supabase configuration. Set ${component.required.join(" and ")} in ${component.name}/.env or your environment, then run make dev again.`
      )
    }
  }
} catch (error) {
  console.error(
    error instanceof SetupError
      ? error.message
      : "Unable to prepare local development configuration."
  )
  process.exitCode = 1
}
