# Workspace naming

Whenever a new Codex task is opened in this workspace, set its task title to exactly `Trellis` before doing other work.
https://github.com/gokuljs

# Repository guidance

This repository contains the Trellis frontend and backend. Keep changes scoped
to the requested area and preserve the existing frontend toolchain. Backend
work lives under `backend/`; read its nested `AGENTS.md` before editing files
there.

## Pstack workflow

- Use the project-local [pstack skill](.agents/skills/pstack/SKILL.md) for
  nontrivial engineering work and explicit pstack requests. It is the only
  engineering workflow entrypoint.
- Use [grill-me](.agents/skills/grill-me/SKILL.md) or
  [grilling](.agents/skills/grilling/SKILL.md) for design interviews that
  challenge plans, assumptions, and tradeoffs. These complement pstack.
- Read the official workflow and only the relevant playbooks and principle
  files from `.agents/pstack/upstream/`. Follow the mappings in the pstack
  skill for Codex tools and user authorization.
- Preserve the pinned upstream snapshot. Keep Trellis guidance in the pstack
  adapter, its references, and this file.
- Preserve the user's scope. A pstack playbook does not authorize creating a
  pull request, sending messages, deploying, or merging unless requested.

## Frontend guidance

- Read [frontend/AGENTS.md](frontend/AGENTS.md) before frontend changes.
- Preserve Trellis's existing visual system unless a redesign is requested.
  Use pstack's relevant design and prototype workflows when they help answer
  the request. Do not add spacing changes without a design reason.
- Keep the existing frontend toolchain and use the relevant checks for changed
  packages and user-facing behavior.

## SVG icon ownership and reuse

- Store every custom SVG icon definition in `frontend/packages/ui/src/icons/`,
  including SVG React components and any standalone SVG icon files. Keep icon
  styles and supporting assets in that same folder.
- Before creating or changing an icon, check that folder for an existing icon
  to reuse. Use component props for size, color, and other supported variants;
  do not copy SVG markup or create duplicate icons in feature components.
- Export and import shared icon components through `@trellis/ui/icons/<name>`.
  Custom `<svg>` definitions must not appear outside the shared icon folder;
  the frontend ESLint configurations enforce this for JSX.
- Keep shared icons independent of app-specific imports. Use relative imports
  for their supporting assets. Existing third-party `lucide-react` icons may
  continue to be imported directly.

## Backend architecture boundaries

- Keep FastAPI routing and HTTP serialization in `backend/app/api`.
- Keep application orchestration separate from transport concerns as runtime
  features are introduced.
- Keep domain models and typed events independent of FastAPI and provider SDKs.
- Put model/provider integrations behind narrow protocols and adapters.
- Do not add authentication, CORS, persistence, queues, containers, or provider
  SDKs until a task explicitly requires them.

## Development and review rules

- Follow pstack's focused `tdd` workflow when a behavior change has a clear,
  inexpensive test path. Otherwise choose the closest practical verification.
- For defects, use the pstack bug-fix workflow and trace the cause before
  changing code.
- Keep async work bounded, timeout-aware, and cancellation-safe. Never hide
  blocking work in an async path operation.
- Prefer the existing dependency policy and justify every new runtime package.
- Review public contracts, error handling, data boundaries, cancellation,
  coverage, and dependency risk when those areas are in scope.
- Prove the requested result with fresh evidence before claiming completion.

## Required backend verification

From `backend/`, run `make check` with uv 0.12.5. The aggregate check runs Ruff
formatting, Ruff linting, and ty.
