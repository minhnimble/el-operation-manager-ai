.PHONY: dev up down migrate seed test lint

dev:
	uvicorn app.main:app --reload --port 8000

up:
	docker-compose up -d db redis

down:
	docker-compose down

migrate:
	alembic upgrade head

migrate-create:
	@read -p "Migration name: " name; alembic revision --autogenerate -m "$$name"

seed:
	python -m scripts.seed_dev

worker:
	celery -A app.tasks.celery_app worker --loglevel=info

beat:
	celery -A app.tasks.celery_app beat --loglevel=info

test:
	pytest tests/ -v --asyncio-mode=auto

lint:
	ruff check app/ && mypy app/

install:
	pip install -r requirements.txt

setup: install up migrate
	@echo "Dev environment ready. Copy .env.example to .env and fill in credentials."
