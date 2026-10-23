UV ?= uv
LOGGING_COVERAGE = app/infrastructure/logging.py,app/api/request_logging.py,app/api/runtime_logging.py
RECOVERY_COVERAGE = app/domain/recovery.py,app/application/recovery_contracts.py,app/application/recovery_discovery.py,app/application/execution_snapshots.py

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
	$(UV) run --locked pytest --cov=app.domain.recovery --cov=app.application.recovery_contracts --cov=app.application.recovery_discovery --cov=app.application.execution_snapshots --cov-fail-under=0
	$(UV) run --locked coverage report --include='$(LOGGING_COVERAGE)' --fail-under=90
	$(UV) run --locked coverage report --include='$(RECOVERY_COVERAGE)' --fail-under=85

check: format-check lint typecheck test
