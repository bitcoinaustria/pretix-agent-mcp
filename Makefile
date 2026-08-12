.PHONY: setup test lint fmt ci tools serve

setup:                     ## install the package and dev dependencies into .venv
	uv venv
	uv pip install -e ".[dev]"

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

ci: lint test             ## what CI runs; run this before opening a PR

tools:                     ## list the tools the current configuration exposes
	uv run pretix-agent-mcp tools

serve:
	uv run pretix-agent-mcp serve
