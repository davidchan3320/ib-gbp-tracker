.PHONY: dev test lint format openapi postgres docker-build docker-up docker-down

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

docker-build:
	docker compose build app

docker-up:
	docker compose up -d app

docker-down:
	docker compose down
