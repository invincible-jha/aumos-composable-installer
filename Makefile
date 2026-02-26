.PHONY: install test test-quick lint format typecheck clean all docker-build

all: lint typecheck test

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v --cov=aumos_composable_installer --cov-report=term-missing

test-quick:
	pytest tests/ -x -q --no-header

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

typecheck:
	mypy src/aumos_composable_installer/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf dist/ build/ *.egg-info

docker-build:
	docker build -t aumos/composable-installer:dev .
