.PHONY: install test lint type cov fmt build smoke clean

install:
	uv sync --all-extras

test:
	uv run pytest -m "not integration and not e2e"

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type:
	uv run mypy src

cov:
	uv run pytest -m "not integration and not e2e" --cov=fleet --cov-report=term-missing

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

build:
	docker build -t fleet:dev .

smoke:
	bash deploy/k8s-smoke.sh

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
