.PHONY: help install lint fmt test eval eval-compare run demo up down

PAPER_ENV := PAPER_TRADING=true EXCHANGE_SANDBOX=true

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dev dependencies
	pip install -r requirements-dev.txt

lint: ## Run ruff
	ruff check .

fmt: ## Auto-fix lint issues
	ruff check --fix .

test: ## Run the test suite (excludes slow/testnet)
	$(PAPER_ENV) pytest -m "not slow"

eval: ## Run the analyst golden-set evaluation
	$(PAPER_ENV) python scripts/eval.py --min-accuracy 0.6

eval-compare: ## Compare accuracy across all prompt versions (A/B)
	$(PAPER_ENV) python -m app.eval.run --all

run: ## Run the API locally (reload)
	$(PAPER_ENV) uvicorn app.main:app --reload

demo: ## Replay the 5 demo scenarios against a running server
	python scripts/demo.py

up: ## docker compose up (app + redis)
	docker compose up --build

down: ## docker compose down
	docker compose down
