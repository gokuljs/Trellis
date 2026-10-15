# Pstack in Trellis

Trellis uses Lauren Tan's official pstack as its single repository-local agent
skill. Pstack selects how to investigate, plan, implement, and verify work. The
root and nested `AGENTS.md` files retain Trellis-specific frontend, backend,
icon, architecture, and verification constraints.

## Source and entrypoint

- Use the discoverable [`pstack` skill](../.agents/skills/pstack/SKILL.md), or
  ask Codex to use `$pstack` or `poteto-mode` for a substantial task.
- The [official source snapshot](../.agents/pstack/upstream/) is pinned to
  `cursor/plugins` commit `850d7f2e9f5e55dff7eb00a0a479337fe79eb22f`, version
  `0.15.25`. Its license and update procedure are kept alongside it.
- The upstream tree contains skills and playbooks for pstack to load when
  needed. Only the native adapter is registered for repository discovery.
- The adapter maps Cursor-specific tools and model roles to supported Codex
  capabilities. It preserves the user's authorization and current model
  settings. The original plugin and automation manifests do not activate tools.

## Use pstack

Start a new chat in this worktree or reopen it so Codex discovers the repository
skill. Example requests:

```text
Use $pstack to investigate how chat streaming works. Keep this read-only.
Use $pstack to reproduce and fix this bug, then verify the behavior.
Use $pstack to review this design before implementation.
Use $pstack to improve the chat composer while preserving Trellis's design.
```

Pstack routes to the appropriate upstream playbook. It loads focused source
references instead of treating every vendored file as a separate local
integration. Local `AGENTS.md` files specify what application boundaries and
checks apply. Diagnosis-only, design-only, and review-only requests keep those
scope limits.

For frontend work, preserve the Bun, Turbo, Vite, React, and TypeScript stack and
read [`frontend/AGENTS.md`](../frontend/AGENTS.md). It records Trellis's visual
system, React constraints, and shared icon rules alongside pstack workflow
routing. For backend work, read [`backend/AGENTS.md`](../backend/AGENTS.md),
including the required `make check` command and pinned uv version.

## Extend frontend guidance inside pstack

Keep the discoverable skill catalog to one pstack entrypoint. When future
frontend work needs additional reusable guidance, place a focused Markdown
reference in `.agents/skills/pstack/references/`. Link it from the pstack entry
point and frontend instructions only when its trigger applies. Add executable
helpers only when they have a clear maintenance benefit, and verify them.

If a future request explicitly calls for more pstack workflows, evaluate the
matching official source and Codex support first. Keep the official source
immutable, preserve provenance, and update the pstack adapter to route into the
new reference. Do not restore standalone third-party skill catalogs or their
installer locks.

## Verification

From the repository root, verify the snapshot:

```bash
(cd .agents/pstack && shasum -a 256 -c upstream.sha256)
```

When guidance or routing changes, validate pstack frontmatter, all local links,
formatting, and that the only repository-local discoverable skill is pstack.
When the app changes, run the checks required for the changed package. Backend
code, dependencies, or configuration changes require `make check` from `backend/`
with uv `0.12.5` as specified in `backend/AGENTS.md`.
