PYTHON ?= .venv/bin/python
COMPOSE ?= docker compose

.PHONY: up down migrate seed logs smoke test test-e2e test-all benchmark-smoke lint build local-install local-migrate local-api local-worker local-smoke

up:
	$(COMPOSE) up --build --wait

down:
	$(COMPOSE) down

migrate:
	$(COMPOSE) run --rm migrate python -m alembic upgrade head

seed:
	$(COMPOSE) run --rm migrate python -m bridge.seed

logs:
	$(COMPOSE) logs -f app worker

smoke:
	$(COMPOSE) exec -T app python -m scripts.run_smoke

benchmark-smoke:
	$(COMPOSE) exec -T worker python -m benchmarks.run --suite smoke --mode hybrid --seed 42 --out /tmp/browserbridge-smoke

test:
	$(PYTHON) -m pytest -q -m 'not browser'

test-e2e: build
	REQUIRE_BROWSER=1 $(PYTHON) -m pytest -q -m browser

test-all: build
	REQUIRE_BROWSER=1 $(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check bridge benchmarks migrations scripts tests
	$(PYTHON) -m ruff format --check bridge benchmarks migrations scripts tests
	cd console && npm run typecheck

build:
	cd console && npm run build

local-install:
	bash scripts/bootstrap.sh

local-migrate:
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m bridge.seed

local-api:
	$(PYTHON) -m uvicorn bridge.main:app --host 127.0.0.1 --port 8000

local-worker:
	$(PYTHON) -m bridge.worker

local-smoke:
	bash scripts/local_smoke.sh
