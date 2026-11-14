# Convenience targets for Linux, macOS and WSL. Every target is a thin wrapper around a
# plain command; on Windows without make, run the commands shown in docs/DEVELOPMENT.md.

.DEFAULT_GOAL := help
BACKEND := backend

.PHONY: help up down logs ps migrate seed reconcile schema-doc test lint format typecheck check

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 14

up: ## Build and start database, migrations and backend
	docker compose up --build -d

down: ## Stop the stack (data is kept)
	docker compose down

logs: ## Follow backend logs
	docker compose logs -f backend

ps: ## Show service status
	docker compose ps

migrate: ## Apply database migrations in the running stack
	docker compose run --rm migrate alembic upgrade head

seed: ## Insert missing initial configuration
	docker compose run --rm migrate python -m app.cli seed

reconcile: ## Verify wallet balances against the ledger
	docker compose run --rm migrate python -m app.cli reconcile

schema-doc: ## Regenerate docs/database/schema.sql from the migrated database
	docker compose exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" \
	  --schema-only --no-owner --no-privileges --exclude-table=alembic_version' \
	  | python3 infrastructure/scripts/schema_doc.py > docs/database/schema.sql

test: ## Run the backend test-suite (needs a PostgreSQL test database)
	cd $(BACKEND) && uv run pytest

lint: ## Lint and check formatting
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

format: ## Apply formatting and safe lint fixes
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .

typecheck: ## Static type checking
	cd $(BACKEND) && uv run mypy

check: lint typecheck test ## Everything CI runs
