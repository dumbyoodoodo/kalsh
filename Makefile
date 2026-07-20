.PHONY: check lint typecheck test fmt sync

check: lint typecheck test

# --no-editable works around a local toolchain quirk (see docs/adr/0001):
# this CPython 3.12 build skips underscore-prefixed .pth files, which breaks
# hatchling's default editable install. --reinstall-package is required
# because non-editable installs are a snapshot copy that otherwise goes
# stale between edits (uv's own staleness check doesn't rebuild it).
sync:
	uv sync --no-editable --reinstall-package kalshi-weather

lint:
	uv run --no-editable ruff check .

typecheck:
	uv run --no-editable mypy src

test: sync
	uv run --no-editable pytest

fmt:
	uv run --no-editable ruff format .
	uv run --no-editable ruff check --fix .
