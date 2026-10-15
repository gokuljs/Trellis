# Codex execution mappings

Read this before applying an official pstack workflow. The source is Lauren
Tan's `cursor/plugins` pstack, version `0.15.25`, at commit
`850d7f2e9f5e55dff7eb00a0a479337fe79eb22f`. It is a reference snapshot, not an
installed Cursor runtime. Read repository instructions first.

## Tools and delegation

| Upstream concept                                                 | Native Codex behavior                                                                                                                                                                                      |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Task`, `poteto-agent`, or other named upstream agent roles      | Use `collaboration.spawn_agent` when delegation helps. Read the named source prompt and include the concrete scope, relevant files, checks, and expected report. There is no registered pstack agent type. |
| Cursor model roles, slugs, `setup-pstack`, or Cursor rule files  | Inherit the current Codex model settings. Do not create or change Cursor configuration or user model preferences.                                                                                          |
| `arena`, `swarm`, or multi-model review                          | Use independent agents for separate coverage or alternatives when useful. Report inherited-model coverage accurately; do not claim model diversity when agents share the same model.                       |
| `AskQuestion`                                                    | Use a native input tool only for a genuine product or preference choice. Inspect code, run a check, or prototype to answer empirical questions.                                                            |
| Todo tools                                                       | Use an available planning tool or keep a concise written plan. Do not invent tool calls or require verbatim imported task lists.                                                                           |
| Cursor `/loop`, cloud workers, `drive`, or orchestration scripts | Use supported tools for a bounded task. Do not claim unavailable sessions or start persistent execution from the reference alone.                                                                          |
| Cursor-only companions and tools                                 | Inspect the original reference and use a supported Codex tool when it fits. Do not claim Cursor-only tools are installed.                                                                                  |

Give independent writers disjoint files or isolated checkouts. Serialize shared
writes. Set a clear stopping condition and verification scope. Review their actual
changes and evidence. Cancel obsolete work and stop processes started for the
task. A missing agent result is a gap, not a pass.

## Scope and external effects

An upstream playbook does not grant permission. Send messages, update tickets,
create pull requests, deploy, or merge only when the user's request authorizes
that action. A playbook may end with preparing a reviewable result; preserve the
requested delivery scope. Do not install packages, execute upstream helpers,
activate plugins, or start automations just because a playbook names them.
Inspect a helper before using it and use it only when its effects are authorized
and needed.

## Trellis verification and frontend work

Use the official pstack `tdd`, bug-fix, review, and proof workflows as applicable,
with repository checks from the nearest `AGENTS.md`. Skip tests whose path is
unclear or expensive and choose the closest practical evidence instead.

Keep frontend work compatible with Trellis's Bun, Turbo, Vite, React, and
TypeScript stack. Read the scoped frontend guidance in `frontend/AGENTS.md`.
Apply upstream React or composition guidance when it fits. Skip Next.js, React
Server Components, and server-action assumptions unless the application uses
them. Use the upstream visual-parity playbook only when an established baseline
makes pixel equivalence the requested outcome.
