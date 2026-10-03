.PHONY: dev test-dev-launcher format format-check

UV ?= uvx --from uv==0.12.5 uv

dev:
	@./scripts/dev-all.sh

test-dev-launcher:
	@./tests/test_dev_launcher.sh

format:
	cd frontend && bun run format
	$(UV) --directory backend run --locked ruff format .

format-check:
	cd frontend && bun run format:check
	$(UV) --directory backend run --locked ruff format --check .
