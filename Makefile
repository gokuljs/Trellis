.PHONY: dev dev-telemetry format format-check setup-hooks

UV ?= uvx --from uv==0.12.5 uv

dev:
	@./scripts/dev-all.sh

dev-telemetry:
	@TRELLIS_TELEMETRY_ENABLED=true TRELLIS_TELEMETRY_EXPORTER=console ./scripts/dev-all.sh

format:
	cd frontend && bun run format
	$(UV) --directory backend run --locked ruff format .

format-check:
	cd frontend && bun run format:check
	$(UV) --directory backend run --locked ruff format --check .

setup-hooks:
	git config --local extensions.worktreeConfig true
	git config --worktree core.hooksPath .githooks
	@printf '%s\n' 'Installed the repository pre-commit hook for this worktree.'
