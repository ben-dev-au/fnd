.PHONY: sync test lint fmt tui snapshot install-hooks

sync:
	uv sync --all-extras --group dev

test:
	uv run pytest -q

test-fast:
	uv run pytest -q -m "not slow"

lint:
	uv run ruff check .
	uv run pyright

fmt:
	uv run ruff format .
	uv run ruff check --fix .

tui:
	uv run textual run --dev fnd.tui.app:FNDApp

snapshot:
	uv run pytest --snapshot-update

install-hooks:
	uv run pre-commit install
