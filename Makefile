.DEFAULT_GOAL := help

.PHONY: install test test-integration lint fmt fmt-check requirements clean help

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

requirements: ## Export production requirements.txt for the app
	uv export --no-dev --no-hashes -o app/requirements.txt

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
