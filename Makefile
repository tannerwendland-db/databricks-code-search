.DEFAULT_GOAL := help

.PHONY: install test test-integration lint fmt fmt-check requirements clean help migrate migrate-local migration set-secrets

install: ## Install all dependencies (incl. dev group)
	uv sync --all-groups

test: ## Run unit tests
	uv run pytest -m unit

test-integration: ## Run integration tests
	uv run pytest -m integration

lint: ## Run ruff check + format check + mypy
	uv run ruff check . && uv run ruff format --check . && uv run mypy app indexer

fmt: ## Auto-format code with ruff
	uv run ruff format .

fmt-check: ## Check formatting without modifying files
	uv run ruff format --check .

migrate: ## Apply schema migrations (Lakebase or PGHOST); grants opt-in via ARGS=--apply-grants
	uv run python scripts/migrate.py $(ARGS)

migrate-local: ## Apply migrations against local Postgres (run under PGHOST; grants skipped)
	uv run python scripts/migrate.py

migration: ## Autogenerate a revision (local only): make migration MSG="message"
	@test -n "$(MSG)" || (echo "migration requires MSG=\"...\"" && exit 1)
	@test -n "$$PGHOST" || (echo "migration requires PGHOST (local); never autogenerate against live Lakebase" && exit 1)
	uv run alembic revision --autogenerate -m "$(MSG)"

set-secrets: ## Write the GitHub token into the bundle's secret scope (run after `bundle deploy`). Requires GITHUB_TOKEN in env.
	@test -n "$$GITHUB_TOKEN" || (echo "set-secrets requires GITHUB_TOKEN in env" && exit 1)
	databricks secrets put-secret code-search github_token --string-value "$$GITHUB_TOKEN"

requirements: ## Export production requirements.txt for the app
	uv export --no-dev --no-hashes -o app/requirements.txt

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
