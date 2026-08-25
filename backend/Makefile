UV ?= uv

.PHONY: dev format format-check lint typecheck test check

dev:
	$(UV) run --locked fastapi dev

format:
	$(UV) run --locked ruff format .

format-check:
	$(UV) run --locked ruff format --check .

lint:
	$(UV) run --locked ruff check .

typecheck:
	$(UV) run --locked ty check

test:
	$(UV) run --locked pytest

check: format-check lint typecheck test
