.PHONY: help install ui test lint fmt demo serve build clean

help:
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install the package and dev dependencies
	pip install -e ".[dev]"
	npm --prefix ui install

ui: ## Build the dashboard into src/swarmdeck/ui
	npm --prefix ui run build

test: ## Run the test suite
	pytest -q

lint: ## Lint and check formatting
	ruff check src tests
	ruff format --check src tests
	npm --prefix ui run typecheck

fmt: ## Auto-format
	ruff check --fix src tests
	ruff format src tests

demo: ## 500 simulated agents in the terminal
	swarmdeck demo --agents 500 --concurrency 100

serve: ui ## Build the UI and start the dashboard
	swarmdeck serve --specs src/swarmdeck/examples --open

build: ui ## Build a wheel with the dashboard included
	python -m build

clean:
	rm -rf dist build src/swarmdeck/ui ui/dist .pytest_cache .ruff_cache .swarmdeck
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
