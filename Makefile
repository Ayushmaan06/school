.PHONY: dev db migrate lint fmt test rebuild eval

# Postgres 16 only. ADR-012 permits Compose with nothing else in it.
db:
	docker compose up -d

migrate:
	uv run alembic upgrade head

dev:
	uv sync --all-groups
	$(MAKE) lint test

lint:
	uv run ruff check school_intel tests
	uv run ruff format --check school_intel tests

fmt:
	uv run ruff format school_intel tests
	uv run ruff check --fix school_intel tests

test:
	uv run pytest -q

# M1-6: truncates ONLY canonical tables and replays. Never touches
# observations / fetches / raw_documents. ADR-009.
rebuild:
	uv run python -m school_intel.cli resolve --rebuild

eval:
	uv run python -m school_intel.cli eval

# M2-1/M2-2/M2-2b: MPD discovery + fee extraction over the campaign geographies.
enrich:
	uv run python -m school_intel.cli enrich

# The worker drains the queue, including the "Get more details" button's jobs.
worker:
	uv run python -m school_intel.cli worker

serve:
	uv run python -m school_intel.cli serve
