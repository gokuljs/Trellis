---
name: pstack
description: Apply Lauren Tan's pstack workflows in Codex for substantial engineering work, including investigation, design, implementation, parallel work, and verification. Use for explicit pstack or poteto-mode requests; skip unrelated prose drafts and trivial edits unless requested.
---

# Pstack for Trellis

This is Trellis's repository-local engineering workflow entrypoint. It routes
to Lauren Tan's official source, pinned under `../../pstack/upstream/`. Follow
repository instructions and the user's task scope. The adapter maps Cursor
assumptions to supported Codex tools; it does not change the upstream snapshot.

## Documentation scope

Create planning documents, decision logs, audit trails, branch trackers, and
architecture, verification, or handoff reports only when the user explicitly
requests them. Long, autonomous, or multi-phase work does not trigger these
artifacts or automatically activate `show-me-your-work`. This overrides upstream
requirements to create or commit those artifacts.

Keep routine plans, progress, decisions, and verification summaries in chat.
Continue running required checks and update existing setup or API documentation
when a code change needs it.

## Choose the workflow

Read [Codex execution mappings](references/codex-adapter.md), then load the
[official mode](../../pstack/upstream/skills/poteto-mode/SKILL.md). Follow its
workflow selection and read only the relevant playbook and principle leaves.
Resolve references within an upstream file relative to that file.

- For an investigation, read [how](../../pstack/upstream/skills/how/SKILL.md)
  and the [investigation playbook](../../pstack/upstream/skills/poteto-mode/playbooks/investigation.md).
  Read [why](../../pstack/upstream/skills/why/SKILL.md) when the question asks
  why a decision was made.
- For new behavior, follow the [feature playbook](../../pstack/upstream/skills/poteto-mode/playbooks/feature.md).
  For a defect, follow [bug-fix](../../pstack/upstream/skills/poteto-mode/playbooks/bug-fix.md),
  [`tdd`](../../pstack/upstream/skills/tdd/SKILL.md) when a focused test is
  practical, and [Fix Root Causes](../../pstack/upstream/skills/principle-fix-root-causes/SKILL.md).
- For a structure-only change, use [refactoring](../../pstack/upstream/skills/poteto-mode/playbooks/refactoring.md).
- For architecture, parallel design, or review, use
  [architect](../../pstack/upstream/skills/architect/SKILL.md),
  [swarm](../../pstack/upstream/skills/swarm/SKILL.md), or
  [interrogate](../../pstack/upstream/skills/interrogate/SKILL.md) when the
  task warrants that depth. Use the Codex mappings when model diversity is
  unavailable.
- When a requested property needs formal verification beyond ordinary tests,
  read [create-formal-verification-skill](../../pstack/upstream/skills/create-formal-verification-skill/SKILL.md).
  Keep generated verification guidance in this adapter's focused references.
- To bring existing formal checks back in line with changed code, read
  [maintain-formal-verification-skill](../../pstack/upstream/skills/maintain-formal-verification-skill/SKILL.md).
- To check a TLA+ model and explain its results as paths through states, read
  [statechart](../../pstack/upstream/skills/statechart/SKILL.md).
  Apply the adapter's helper and rendering mappings before running its tools.
- For an empirical design choice, use [prototype](../../pstack/upstream/skills/poteto-mode/playbooks/prototype.md).
  Use [visual parity](../../pstack/upstream/skills/poteto-mode/playbooks/visual-parity.md)
  only when the task compares implementations against an established visual
  baseline. Do not use these workflows to expand the requested scope.

## Preserve Trellis constraints

Read the root and nearest `AGENTS.md` before editing. They define the product's
frontend toolchain, shared icon ownership, backend architecture, and required
checks. Apply those constraints through pstack. Complementary design interviews
use [grill-me](../grill-me/SKILL.md) and [grilling](../grilling/SKILL.md).

Read [frontend guidance](../../../frontend/AGENTS.md) before frontend work.
Keep Trellis's visual system unless the user asks for a redesign. For frontend
polish, motion, mobile behavior, or UI exploration, read
[frontend craft](references/frontend-craft.md) and load only the relevant
complementary skill leaves within the selected pstack workflow.

For backend changes, preserve the transport, application, domain, and provider
boundaries. Run the required backend checks whenever backend code, dependencies,
or configuration change. For completion, follow [Prove It Works](../../pstack/upstream/skills/principle-prove-it-works/SKILL.md).

Keep upstream files immutable. Put Codex and Trellis adaptations in this adapter,
its references, or repository instructions, and record source updates separately.
