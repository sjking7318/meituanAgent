.PHONY: install format lint typecheck test test-int check dev worker migrate downgrade seed up up-core down

install:
	uv sync --all-extras

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest -m "not integration" --cov=sales_assistant --cov-report=term-missing

test-int:
	uv run pytest tests/integration -m integration

check: lint typecheck test

dev:
	uv run uvicorn sales_assistant.main:app --reload --host 0.0.0.0 --port 8000

worker:
	uv run python -m sales_assistant.workers.main

migrate:
	uv run alembic upgrade head

downgrade:
	uv run alembic downgrade -1

seed:
	uv run python -m sales_assistant.scripts.seed

up:
	docker compose up -d mysql redis etcd minio milvus redpanda

up-core:
	docker compose up -d mysql redis

down:
	docker compose down
