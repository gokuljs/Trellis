# Trellis frontend instructions

Read the repository-root `AGENTS.md` first. Use the pstack skill for nontrivial
frontend work and select only the relevant workflow from the official snapshot.
Preserve the user's requested scope and do not treat a review or design request
as permission to implement a change.

## Preserve the application

- Keep the existing Bun, Turbo, Vite, React, and TypeScript toolchain.
- Preserve Trellis's established visual system unless the user requests a
  redesign. For a design decision that can be answered by a prototype, use
  pstack's `prototype` workflow. Use `visual-parity` only for pixel-equivalence
  work with an established baseline.
- Inspect `packages/ui/src/icons/` before adding icons. Custom SVG definitions
  belong there and are imported through `@trellis/ui/icons/<name>`.
- Choose checks that match the change. For user-facing work, inspect the actual
  interface and relevant states in addition to source-level checks.
- Apply upstream React and composition guidance when it fits Trellis. Skip
  Next.js, React Server Components, and server-action assumptions unless the
  application actually uses them.

## Add frontend guidance to pstack

Keep frontend engineering workflows in the pstack entrypoint. The shared
grill-me and grilling skills provide complementary design interviews.
For frontend polish, motion, mobile behavior, or UI exploration, use pstack's
[frontend craft reference](../.agents/skills/pstack/references/frontend-craft.md).
When a future task needs more frontend guidance, add a focused reference beneath
`.agents/skills/pstack/references/` and route to it from the pstack entrypoint.
Add complementary frontend craft skills and their installer lock only when
the user explicitly requests installation. Keep pstack as the engineering
workflow entrypoint.
