# Agent workflows in Trellis

Trellis combines Lauren Tan's pstack engineering workflow with its existing
domain skills. Pstack selects a way to investigate, plan, implement, and verify
work. Frontend and backend skills supply the project-specific decisions.

## What is installed

- The native entrypoint is [`.agents/skills/pstack/SKILL.md`](../.agents/skills/pstack/SKILL.md).
- The original source is [`.agents/pstack/upstream/`](../.agents/pstack/upstream/).
  It is pinned to `cursor/plugins` commit
  `850d7f2e9f5e55dff7eb00a0a479337fe79eb22f`, pstack `0.15.25`.
- [The provenance manifest](../.agents/pstack/upstream.json) and
  [snapshot maintenance guide](../.agents/pstack/README.md) track the source
  and explain how to verify or update it.
- Existing skills, installation locks, app dependencies, and build commands
  retain their current ownership. This integration adds no runtime packages.

The canonical source is [Lauren's Cursor plugin](https://github.com/cursor/plugins/tree/main/pstack),
not a community port. Its original tool and model assumptions need adaptation
for Codex. Keep those adaptations in the native entrypoint rather than editing
the imported files. The complete upstream tree includes tools and automations
for reference; they are not enabled by this integration.

## Use it

Start a new Codex chat in this worktree or reopen it to refresh skill discovery.
Codex discovers native skills under `.agents/skills/`; if a running chat has a
stale catalog, ask it to read the adapter's path explicitly.

Example requests:

```text
Use $pstack to investigate how chat streaming works. Keep this read-only.
Use $pstack to fix this bug with a reproducer and focused verification.
Use $pstack and Impeccable to improve the chat composer spacing.
Use $pstack to review this design before implementing it.
```

`pstack` is the installed skill name. Requests mentioning `poteto-mode` also
route to it. The original Cursor `/add-plugin` and `/setup-pstack` commands do
not configure this Codex integration. Subagents use the current model settings
unless the user has requested supported overrides. Independent reviewers on
the same model are not described as a diverse-model panel.

The adapter reads only the relevant upstream workflow, playbook, and principle
references. It pairs these with the domain skills below. It does not register
every upstream leaf as a separate slash command.

## Skill ownership

| Work                                                        | Local owner                            |
| ----------------------------------------------------------- | -------------------------------------- |
| Multi-step engineering, investigations, and pstack requests | Root `pstack` adapter                  |
| Frontend design, layout, spacing, and visual polish         | Root `impeccable`                      |
| Requested UI, UX, and accessibility audits                  | Root `web-design-guidelines`           |
| React rendering and performance                             | Frontend `vercel-react-best-practices` |
| Reusable React APIs and composition                         | Frontend `vercel-composition-patterns` |
| Requested prose audits                                      | Frontend `writing-guidelines`          |
| FastAPI and Pydantic boundaries                             | Root `fastapi`                         |
| Trellis runtime and provider contracts                      | Root `develop-trellis-runtime`         |
| Bug investigation                                           | Root `systematic-debugging`            |
| Behavior changes and reproducing tests                      | Root `test-driven-development`         |
| Independent code review                                     | Root `requesting-code-review`          |
| Completion claims and checks                                | Root `verification-before-completion`  |
| Agent-facing controlled language                            | Root `asd-ste100`                      |
| Visual explanations                                         | Root `show-me`                         |

Read [root instructions](../AGENTS.md), then the instructions nearest the changed
files. [Frontend instructions](../frontend/AGENTS.md) give explicit skill paths,
including nested skills that a root-started chat may not list automatically.
[Backend instructions](../backend/AGENTS.md) retain their architecture and
verification requirements.

The root and frontend directories already contain copies of
`web-design-guidelines`. Route to the root copy; do not run two audits or merge
their separate lock entries as part of unrelated work.

Pstack's TDD, root-cause, and proof workflows reinforce the existing local
skills. Read the local owner rather than following stale `superpowers:`
namespace aliases or running a second conflicting workflow. Cursor-only
companions such as `deslop` and `control-ui` are not installed here. Use the
native adapter's mapping and report unavailable capabilities accurately.

## Adding skills

1. Choose the scope. Shared engineering skills go in
   `.agents/skills/<name>/`. Frontend-only skills go in
   `frontend/.agents/skills/<name>/`.
2. Add a `SKILL.md` whose `name` matches its folder. Use a unique lowercase
   kebab-case name. State a specific trigger so unrelated tasks do not load it.

   ```markdown
   ---
   name: frontend-example
   description: Use when implementing the specific frontend capability this skill owns.
   ---

   # Frontend example

   Describe the decisions and project constraints this capability needs.
   Link detailed references only where they apply.
   ```

3. Add the task and exact path to `frontend/AGENTS.md` for frontend skills, or
   root `AGENTS.md` for shared skills. Keep visual design owned by Impeccable
   unless the user explicitly requests a new design workflow. New skills do
   not require changes to the pstack snapshot or adapter.
4. Preserve third-party licensing and record the source and reviewed revision.
   For skills installed through the skills CLI, keep its generated lock entry
   in the matching root or frontend `skills-lock.json`. Local skills and this
   pstack adapter do not need invented installer hashes.
5. Validate required frontmatter, local references, and any executable helpers.
   If the installed `skill-creator` provides `scripts/quick_validate.py`, run
   it on the new skill folder. Check the skill's decisions against realistic
   requests; syntax validation alone does not prove useful routing.

Avoid duplicate skill names across scopes. Do not add frontend skill catalogs
to every pstack playbook. Keep the routing decision in the scoped instructions.

## Verification

For an integration or source update:

```bash
(cd .agents/pstack && shasum -a 256 -c upstream.sha256)
git diff --check -- . ':!.agents/pstack/upstream/**'
```

Also check the snapshot's file list, bytes, executable bits, commit, tree, and
version against the source checkout. Validate the native adapter's frontmatter
and local Markdown links. Keep upstream bytes unchanged instead of applying
Trellis formatting to the imported tree.

Exercise these read-only scenarios with an independent agent that reads the
adapter and scoped instructions:

- Investigate backend behavior. Identify a matching investigation path and
  backend boundaries without editing files or installing packages.
- Plan a composer spacing change. Identify Impeccable and frontend verification
  without changing the application framework.
- Plan a React composition improvement. Find the nested composition skill from
  a repository-root start.
- Plan an ordinary feature. Identify local TDD and completion checks and retain
  the user's delivery scope when the upstream playbook reaches PR creation.

Run application checks when application code changes. Backend changes require
`make check` from `backend/` with uv `0.12.5`, as specified in `backend/AGENTS.md`.
A documentation-only skill integration does not establish that backend or
frontend application tests pass.
