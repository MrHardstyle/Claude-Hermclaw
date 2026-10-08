# Developer shortcuts (P02 2.7/2.8)
PY ?= .venv/bin/python
.PHONY: venv lint format typecheck test test-unit ui schemas
venv:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
lint:
	.venv/bin/ruff check hermclaw worker tests && .venv/bin/ruff format --check hermclaw worker tests
format:
	.venv/bin/ruff format hermclaw worker tests && .venv/bin/ruff check --fix hermclaw worker tests
typecheck:
	.venv/bin/mypy hermclaw worker
test:
	.venv/bin/pytest -q
test-unit:
	.venv/bin/pytest -q tests/unit tests/contract
schemas:
	$(PY) -m hermclaw.contracts.schema_export docs/contracts/schemas
ui:
	cd ui && npm ci && npm run build
