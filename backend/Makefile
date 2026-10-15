# RelaxAI backend developer commands. Requires uv (https://docs.astral.sh/uv/).
UV ?= uv
RUN := $(UV) run --frozen

.PHONY: install lint format typecheck imports test openapi check migrate run

install:            ## Create .venv and install runtime + dev dependencies from uv.lock
	$(UV) sync --frozen

lint:               ## ruff lint + format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:             ## Apply ruff fixes and formatting
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

typecheck:          ## mypy --strict over app, tests, migrations
	$(RUN) mypy

imports:            ## import-linter module boundary contracts
	$(RUN) lint-imports

test:               ## Full test suite (needs TEST_DATABASE_URL or Docker)
	$(RUN) pytest

openapi:            ## Regenerate openapi.json (commit it with any contract change)
	$(RUN) python -m app.api.export_openapi

check: lint typecheck imports test   ## Every quality gate (Definition of Done §9.6)

migrate:            ## Apply migrations to the database in RELAXAI__DB__URL / .env
	$(RUN) alembic upgrade head

run:                ## Start the API on 127.0.0.1:8000
	$(RUN) uvicorn app.main_api:app --host 127.0.0.1 --port 8000
