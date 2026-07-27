.PHONY: dev test lint format openapi postgres

dev:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

openapi:
	uv run python -m scripts.export_openapi

postgres:
	docker compose up -d db
