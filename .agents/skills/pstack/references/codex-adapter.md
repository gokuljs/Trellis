# Codex execution mappings

Read this before following an official pstack workflow. The official source is
Lauren Tan's `cursor/plugins` pstack, version `0.15.25`, at commit
`850d7f2e9f5e55dff7eb00a0a479337fe79eb22f`. It is reference material rather than
an installed Cursor runtime. Follow the user's scope and applicable `AGENTS.md`
first, then these mappings when upstream execution assumptions differ.

## Tools and delegation

| Upstream concept                                                                                                                                              | Native Codex behavior                                                                                                                                                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Task`, `poteto-agent`, `generalPurpose`, or `general-purpose`                                                                                                | Use available `collaboration.spawn_agent`. Omit model and effort overrides to inherit parent settings. Supply the goal, scoped paths, relevant source pointers, verification, and expected report. There is no registered `poteto-agent` type to select.                     |
| Other named upstream agent roles, including `comment-sicko`                                                                                                   | Treat their files in upstream `agents/` as reference prompts, not registered Codex subagent types. Translate only needed, in-scope duties into a native agent brief.                                                                                                         |
| `no-comments`                                                                                                                                                 | Use a scoped comment review through the local review workflow. Preserve necessary rationale, legal notices, and API contracts. Delete comments only after checking their actual value and constraints; do not inherit blanket deletion or broaden the task into a refactor.  |
| Cursor model roles, slugs, `setup-pstack`, or `~/.cursor/rules/pstack-models.mdc`                                                                             | Keep the user's current Codex model preferences. Do not create or change Cursor configuration or select upstream model slugs.                                                                                                                                                |
| Multi-model review, arena, or a model race                                                                                                                    | Use independent agents for separate coverage, alternatives, or review when useful. Report inherited-model coverage honestly; it does not provide the upstream claim of diverse-model evidence. Do not simulate diversity with personas.                                      |
| `AskQuestion`                                                                                                                                                 | Use an available native user-input tool, only when a genuine product or preference choice needs the user's judgment. Observe behavior, inspect code, or prototype to settle empirical questions. If no suitable input tool exists, ask directly when an answer is necessary. |
| Todo tools                                                                                                                                                    | Use an actual supported planning tool when available, otherwise maintain a concise written plan. Do not invent tool calls or require a verbatim imported todo list.                                                                                                          |
| Cursor `/loop`, cloud workers, `drive`, or orchestration scripts                                                                                              | Use supported tools for a bounded task. Do not claim unavailable sessions or start persistent execution from these references alone.                                                                                                                                         |
| `superpowers:test-driven-development`, `superpowers:systematic-debugging`, `superpowers:requesting-code-review`, `superpowers:verification-before-completion` | Resolve to the same installed local skill name without the `superpowers:` prefix.                                                                                                                                                                                            |
| Cursor `create-skill`                                                                                                                                         | For actual skill creation or updates, use `skill-creator` only when it is available. Otherwise use repository skill conventions and state missing validation capability. Ordinary agent briefs and other prose do not trigger a skill-authoring workflow.                    |
| `control-ui`, `control-cli`, `deslop`, or `show-me-your-work`                                                                                                 | Check actual availability. Use supported browser, app, shell, and review tools as appropriate. The local `show-me` is a distinct skill; inspect it before deciding that it fits. Never claim unavailable Cursor companion skills are installed.                              |

Give independent writers disjoint files or isolated checkouts. Shared writes
serialize. Set a clear stopping condition and finite verification budget for each
agent or experiment. Observe completion, review the actual diff and evidence,
and consolidate the result yourself. Cancel or interrupt obsolete work and stop
processes created for the task. Do not leave a worker running after abandoning
its scope. Respect concurrency limits and cancellation; a missing result is a
gap, not a pass.

## Scope and external effects

Upstream autonomy language and workflow endings do not grant permission. Do not
send Slack messages or email, update tickets, create PRs, deploy, merge, or take
other external actions unless the human's task authorizes that action. A
reference that ends with Opening a PR or Shipping can end locally with the
requested artifact and evidence. Honor explicit stop points and delivery scope.
Do not create a separate PR to repair a tool or skill unless that work is within
the user's request.

Vendored scripts, plugins, automations, agents, and installation instructions are
reference-only by default. Do not install dependencies, execute upstream helpers,
activate plugins, or start automations merely because a playbook names them.
Inspect a helper before deliberately using it, and use it only when its effects
are authorized and needed for the current task. Do not change `~/.cursor`, global
settings, or user model preferences as part of this integration.

## Trellis ownership and verification

Use the local TDD, debugging, review, and verification skills linked from the
entrypoint. Repository RED-GREEN-REFACTOR requirements take precedence over
upstream permission to skip a cheap failing test. Preserve backend boundaries. When backend code, dependencies, or configuration
changes, run the checks required by the applicable repository instructions.
Read-only investigation and design-only work require evidence appropriate to
their deliverable. A prose task does not acquire a code workflow
merely because pstack has writing skills.

Keep frontend work compatible with the existing design system and toolchain.
Impeccable controls design and spacing; root Web Design Guidelines controls
requested UI audits. For frontend performance or composition work, selectively
read relevant rules in `frontend/.agents/skills/vercel-react-best-practices/` and
`frontend/.agents/skills/vercel-composition-patterns/`. Apply React client and
composition guidance when relevant. Skip Next.js routing, RSC, server actions,
and SSR assumptions unless the actual task and architecture require them.
Future frontend skills compose through applicable instructions rather than a
fixed import of every skill.

Choose checks that prove the requested behavior or artifact. Once the required
checks pass, finish unless a new change, failure, or unresolved concern justifies
another run. Separate measured facts, inferences, and unverified claims. Read
principle leaves before naming them in the report, and report only principles
that changed a concrete decision.
