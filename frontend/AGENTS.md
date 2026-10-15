# Trellis frontend instructions

Read the repository-root `AGENTS.md` first. Its shared icon rules and existing
visual system apply throughout this directory.

## Workflow and skill routing

Use the root [pstack adapter](../.agents/skills/pstack/SKILL.md) for multi-step
engineering workflow. Pair it with the relevant frontend skill; pstack does
not select the visual direction or replace domain guidance.

| Task                                                          | Skill to read                                                                  |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Design, layout, spacing, typography, motion, or visual polish | [Root Impeccable](../.agents/skills/impeccable/SKILL.md)                       |
| Requested UI, UX, or accessibility audit                      | [Root Web Design Guidelines](../.agents/skills/web-design-guidelines/SKILL.md) |
| React rendering, data fetching, or performance                | [React best practices](.agents/skills/vercel-react-best-practices/SKILL.md)    |
| Reusable component APIs or composition                        | [Composition patterns](.agents/skills/vercel-composition-patterns/SKILL.md)    |
| Requested documentation or prose audit                        | [Writing guidelines](.agents/skills/writing-guidelines/SKILL.md)               |

The root Web Design Guidelines copy is the routing authority. The existing
frontend copy remains for compatibility; do not load both copies for one audit.
Read a skill when its task applies instead of loading the whole catalog.

## Preserve the application

- Keep the existing Bun, Turbo, Vite, React, and TypeScript toolchain.
- Apply the Vercel skills' relevant React guidance. Next.js, server components,
  server actions, or framework-specific caching advice applies only if that
  technology is actually present and in scope.
- Use Impeccable's layout workflow to improve spacing and rhythm. Preserve
  Trellis's visual system unless a redesign is requested.
- Inspect `packages/ui/src/icons/` before adding icons. Custom SVG definitions
  belong there and are imported through `@trellis/ui/icons/<name>`.
- Use the existing focused checks and the package scripts appropriate to the
  change. For UI behavior, inspect the running interface as well as checking
  code; use Impeccable's verification guidance for the affected surface.

## Add frontend skills

Put frontend-only skills in `.agents/skills/<name>/SKILL.md` beneath this
directory. Use a unique, descriptive name and add its task trigger and path
to the table above so a chat started at the repository root can find it.
Keep shared workflow skills in the root `.agents/skills/` instead.
See [the extension guide](../docs/agent-workflows.md#adding-skills) for the
frontmatter format, provenance, and validation workflow.
