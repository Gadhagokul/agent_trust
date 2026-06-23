.PHONY: install dev lint typecheck test run

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

lint:
	ruff check app tests

typecheck:
	mypy app

test:
	pytest

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload