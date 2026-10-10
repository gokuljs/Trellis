import path from "node:path"
import { ESLint } from "eslint"
import { describe, expect, it } from "vitest"

const webRoot = import.meta.dirname
const uiRoot = path.resolve(webRoot, "../../packages/ui")

const web = new ESLint({
  cwd: webRoot,
  overrideConfigFile: path.join(webRoot, "eslint.config.js"),
  overrideConfig: {
    languageOptions: { parserOptions: { tsconfigRootDir: webRoot } },
  },
})
const ui = new ESLint({
  cwd: uiRoot,
  overrideConfigFile: path.join(uiRoot, "eslint.config.js"),
  overrideConfig: {
    languageOptions: { parserOptions: { tsconfigRootDir: uiRoot } },
  },
})

const inlineIcon = "export function Fixture() { return <svg /> }"
const iconConsumer = `
import { GoogleIcon } from "@trellis/ui/icons/google-icon"
import { Github } from "lucide-react"

export function Fixture() {
  return <><GoogleIcon /><Github /></>
}
`

describe("shared SVG icon policy", () => {
  it.each([
    {
      name: "web components",
      eslint: web,
      filePath: "src/components/icon.tsx",
    },
    { name: "web icons", eslint: web, filePath: "src/icons/icon.tsx" },
    { name: "UI components", eslint: ui, filePath: "src/components/icon.tsx" },
    {
      name: "UI sibling folders",
      eslint: ui,
      filePath: "src/icons-other/icon.tsx",
    },
  ])("rejects inline SVG in $name", async ({ eslint, filePath }) => {
    const [result] = await eslint.lintText(inlineIcon, { filePath })

    expect(result.errorCount).toBe(1)
    expect(result.messages).toEqual([
      expect.objectContaining({
        ruleId: "no-restricted-syntax",
        severity: 2,
      }),
    ])
  })

  it.each(["src/icons/icon.tsx", "src/icons/brands/icon.tsx"])(
    "permits inline SVG in shared %s",
    async (filePath) => {
      const [result] = await ui.lintText(inlineIcon, { filePath })

      expect(result.messages).toEqual([])
    }
  )

  it.each([
    { name: "web", eslint: web },
    { name: "UI", eslint: ui },
  ])(
    "permits shared and Lucide icon consumers in $name",
    async ({ eslint }) => {
      const [result] = await eslint.lintText(iconConsumer, {
        filePath: "src/components/icon-consumer.tsx",
      })

      expect(result.messages).toEqual([])
    }
  )
})
