# Workspace naming

Whenever a new Codex task is opened in this workspace, set its task title to exactly `Trellis` before doing other work.
https://github.com/gokuljs
# Repository guidance

This repository contains the Trellis frontend and backend. Keep changes scoped
to the requested area and preserve the existing frontend toolchain. Backend
work lives under `backend/`; read its nested `AGENTS.md` before editing files
there.

## Backend architecture boundaries

- Keep FastAPI routing and HTTP serialization in `backend/app/api`.
- Keep application orchestration separate from transport concerns as runtime
  features are introduced.
- Keep domain models and typed events independent of FastAPI and provider SDKs.
- Put model/provider integrations behind narrow protocols and adapters.
- Do not add authentication, CORS, persistence, queues, containers, or provider
  SDKs until a task explicitly requires them.

## Development and review rules

- Use RED–GREEN–REFACTOR for behavior changes: write a focused failing test,
  implement the smallest passing change, then refactor.
- Keep async work bounded, timeout-aware, and cancellation-safe. Never hide
  blocking work in an async path operation.
- Prefer the existing dependency policy and justify every new runtime package.
- Prioritize public contract compatibility, error handling, data boundaries,
  cancellation, test coverage, and dependency/security risk during review.
- Do not claim completion without fresh evidence from the required checks.

## Required backend verification

From `backend/`, run `make check` with uv 0.12.5 (or the CI-equivalent pinned
uv invocation). The aggregate check runs Ruff formatting, Ruff linting, ty,
strict pytest, and the 90% coverage floor.
