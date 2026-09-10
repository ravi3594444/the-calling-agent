.PHONY: help install dev test lint fmt docker-build docker-up deploy-up logs

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s "$$(printf '\t')"

install:  ## Create .venv and install with dev extras
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:  ## Run with auto-reload on http://localhost:8080
	.venv/bin/uvicorn calling_agent.main:app --reload --host 0.0.0.0 --port 8080

test:  ## Run the test suite
	.venv/bin/pytest tests/ -q

lint:  ## Lint
	.venv/bin/ruff check src tests

fmt:  ## Format
	.venv/bin/ruff format src tests

docker-build:  ## Build the image
	docker compose build

docker-up:  ## Run the app alone, http on :8080
	docker compose up app

deploy-up:  ## Run app + Caddy TLS (on the server)
	docker compose --profile tls up -d

logs:  ## Tail container logs
	docker compose logs -f
