# Frontend craft in Trellis

Use this reference within the selected pstack workflow for frontend polish,
motion, mobile behavior, or UI exploration. Read the
[frontend instructions](../../../../frontend/AGENTS.md) first. Keep imported
skill files unchanged and put Trellis adaptations in this reference.

## Work with the existing frontend

Preserve the Bun, Turbo, Vite, React, and TypeScript stack recorded in the
[workspace](../../../../frontend/package.json),
[web app](../../../../frontend/apps/web/package.json), and
[shared UI](../../../../frontend/packages/ui/package.json) manifests.
Reuse `@trellis/ui` components and the existing visual tokens. Custom SVG icons
belong in `frontend/packages/ui/src/icons/` and use `@trellis/ui/icons/<name>`
imports.

Reuse the installed Radix primitives and Sonner integration. Toast work starts
from [GlobalToaster](../../../../frontend/apps/web/src/components/global-toaster.tsx)
and the [notification helpers](../../../../frontend/apps/web/src/lib/notifications.ts).
For library selection, read [pick-ui-library](../../pick-ui-library/SKILL.md)
only when explicitly invoked. Its recommendations must respect existing
dependencies and the root dependency policy; justify each new runtime package.

## Load the craft reference that fits the task

| Task                                                     | Relevant skill leaves                                                                                                                                                                                                         |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Polish components or assess physical interaction details | [emil-design-eng](../../emil-design-eng/SKILL.md); [apple-design](../../apple-design/SKILL.md) for gesture, depth, and fluid-motion questions.                                                                                |
| Build web motion or diagnose dropped frames              | [animate](../../animate/SKILL.md); [animation-performance](../../animation-performance/SKILL.md) for rendering cost.                                                                                                          |
| Review a requested animation or plan motion improvements | Explicitly invoked [review-animations](../../review-animations/SKILL.md); [improve-animations](../../improve-animations/SKILL.md) for a codebase audit and plans.                                                             |
| Decide where motion belongs or what an effect is called  | [find-animation-opportunities](../../find-animation-opportunities/SKILL.md), [remove-animations](../../remove-animations/SKILL.md), or [animation-vocabulary](../../animation-vocabulary/SKILL.md), according to the request. |
| Check mobile-web behavior or extreme UI data             | [mobile-native](../../mobile-native/SKILL.md) for browser/touch behavior; [break-ui](../../break-ui/SKILL.md) for realistic edge-case fixtures.                                                                               |
| Build or troubleshoot toast feedback                     | [ask-sonner](../../ask-sonner/SKILL.md).                                                                                                                                                                                      |

For explicitly requested UI variants, use
[Emil's prototype skill](../../prototype/SKILL.md) as a picker and craft reference
inside the [pstack prototype playbook](../../../pstack/upstream/skills/poteto-mode/playbooks/prototype.md).
Keep exploration in an isolated scratch directory. Hand the chosen direction
to pstack's Feature workflow for production implementation within the user's
authorized scope.

Use the frontend's existing lint, typecheck, and build commands as applicable.
Inspect the actual UI states affected by the change, including reduced motion
or touch behavior when those are in scope.
