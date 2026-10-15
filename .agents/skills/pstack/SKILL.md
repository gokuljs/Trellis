---
name: pstack
description: Apply Lauren Tan's pstack workflows in Codex for pstack or poteto-mode requests, or substantial engineering work that needs investigation, design, parallel execution, and verification. Skip unrelated prose drafts and trivial edits unless explicitly invoked.
---

# Pstack for Trellis

Use Lauren Tan's official pstack through this native Codex adapter. The unchanged
source lives outside skill discovery at `../../pstack/upstream/`. Repository
instructions and the user's task scope govern execution; this adapter translates
Cursor assumptions without changing the official snapshot.

## Start selectively

Read [Codex execution mappings](references/codex-adapter.md) before executing an
upstream workflow. Read the [official mode](../../pstack/upstream/skills/poteto-mode/SKILL.md)
for its workflow and principle routing, then load only the playbook and leaf
references that apply. Do not load all upstream skills or treat them as installed
Codex skills. Resolve links inside an upstream file relative to that file.
Choose the workflow by the requested deliverable. A diagnosis-only, review-only,
or design-only request does not authorize implementing a fix or feature.

- Read-only explanation or recommendation: [Investigation](../../pstack/upstream/skills/poteto-mode/playbooks/investigation.md),
  with [how](../../pstack/upstream/skills/how/SKILL.md) and, for rationale, [why](../../pstack/upstream/skills/why/SKILL.md).
- New behavior: [Feature](../../pstack/upstream/skills/poteto-mode/playbooks/feature.md).
  Reproduce a defect with [Bug fix](../../pstack/upstream/skills/poteto-mode/playbooks/bug-fix.md).
- Structural change: [Refactoring](../../pstack/upstream/skills/poteto-mode/playbooks/refactoring.md).
  Settle an empirical design choice with [Prototype](../../pstack/upstream/skills/poteto-mode/playbooks/prototype.md).
- Architecture or contested design: read [architect](../../pstack/upstream/skills/architect/SKILL.md)
  or [interrogate](../../pstack/upstream/skills/interrogate/SKILL.md) when that depth is warranted.
  For parallel work, use [swarm](../../pstack/upstream/skills/swarm/SKILL.md).
- For another explicit pstack mode, find its playbook through the official mode.
  Load [figure-it-out](../../pstack/upstream/skills/figure-it-out/SKILL.md) only when
  a large or unusual task needs a bespoke plan. Long-run and shipping playbooks
  do not expand authorization or start background work by themselves.

Read each principle leaf before applying or citing it. Useful starting points are
[Model the Domain](../../pstack/upstream/skills/principle-model-the-domain/SKILL.md)
for state and data shapes, [Boundary Discipline](../../pstack/upstream/skills/principle-boundary-discipline/SKILL.md)
for adapters, [Separate Before Serializing Shared State](../../pstack/upstream/skills/principle-separate-before-serializing-shared-state/SKILL.md)
for concurrent writes, [Laziness Protocol](../../pstack/upstream/skills/principle-laziness-protocol/SKILL.md)
for scope, and [Prove It Works](../../pstack/upstream/skills/principle-prove-it-works/SKILL.md)
for completion claims. Choose other leaves from the official mode as needed.

## Compose with the project

Use [test-driven-development](../test-driven-development/SKILL.md) for behavior
changes, [systematic-debugging](../systematic-debugging/SKILL.md) for defects,
[requesting-code-review](../requesting-code-review/SKILL.md) for review, and
[verification-before-completion](../verification-before-completion/SKILL.md)
for completion evidence. These local workflows govern their areas when an
upstream shortcut differs. Documentation-only adapter edits need validation,
not a behavior test.

[Impeccable](../impeccable/SKILL.md) owns frontend design, layout, spacing, and
visual decisions. The root [web-design-guidelines](../web-design-guidelines/SKILL.md)
owns requested UI, accessibility, and UX audits. Pstack coordinates the work;
it does not replace either. Discover other existing or future frontend skills
from the applicable repository instructions and touched area, then use only the
relevant guidance. Apply the frontend React and composition skills conditionally
within Trellis's Bun, Turbo, and Vite toolchain; do not introduce Next.js or React
Server Components because an upstream example uses them.

Keep upstream files immutable. Put Codex and Trellis adaptations in this wrapper
or repository guidance, and record upstream updates separately.
