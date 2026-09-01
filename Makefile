.PHONY: help dev test lint migrate build up down worker openapi sdk load-test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Development ──────────────────────────────────────────────────────
dev: ## Start local dev stack (postgres + redis + api + worker)
	docker compose up -d

down: ## Stop local dev stack
	docker compose down -v

# ── Testing ──────────────────────────────────────────────────────────
test: ## Run all tests (unit + integration)
	uv run pytest tests/ -q

test-unit: ## Run unit tests only
	uv run pytest tests/unit -q

test-integration: ## Run integration tests (requires DATABASE_URL)
	uv run pytest tests/integration -q -m integration

test-security: ## Run security-negative suite
	uv run pytest tests/integration -q -m security

test-coverage: ## Run tests with coverage report
	uv run pytest tests/ -q --cov=xiosync --cov-report=term-missing --cov-report=html:coverage-html

# ── Code Quality ─────────────────────────────────────────────────────
lint: ## Run linting, type checking, and architecture rules
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy xiosync/
	uv run lint-imports

format: ## Auto-format code
	uv run ruff check --fix .
	uv run ruff format .

# ── Database ─────────────────────────────────────────────────────────
migrate: ## Run database migrations
	uv run alembic upgrade head

migrate-rollback: ## Rollback last migration
	uv run alembic downgrade -1

# ── Build & Deploy ───────────────────────────────────────────────────
build: ## Build Docker images
	docker compose build

smoke: ## Run CI smoke test
	docker compose -f docker-compose.smoke.yml up --build --abort-on-container-exit
	docker compose -f docker-compose.smoke.yml down -v

# ── Worker ───────────────────────────────────────────────────────────
worker: ## Run background worker (all loops)
	uv run python -m xiosync.worker.main

worker-ticker: ## Run cron trigger ticker only
	uv run python -m xiosync.worker.main --mode ticker

worker-reaper: ## Run lease reaper only
	uv run python -m xiosync.worker.main --mode reaper

# ── Tools ────────────────────────────────────────────────────────────
openapi: ## Export OpenAPI spec to openapi.json
	uv run python tools/export_openapi.py

sdk: ## Generate client SDKs (TypeScript by default)
	uv run python tools/generate_sdk.py --lang all

load-test: ## Run load tests (requires locust: pip install locust)
	locust -f tools/load_test.py --host http://localhost:8000
