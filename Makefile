# Patchora developer tasks (Linux/macOS). On Windows use: powershell -File dev.ps1 <task>
PY ?= .venv/bin/python
NPM ?= npm

.PHONY: help install init-db demo backend frontend dev test test-docker lint typecheck eval eval-docker openapi check-integrations reset-demo docker-up

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | sort

install:            ## create the virtualenv and install backend + frontend dependencies
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	cd frontend && $(NPM) ci

init-db:            ## create/upgrade the SQLite database
	$(PY) backend/scripts/init_db.py

demo:               ## seed synthetic demo data (no credentials needed)
	$(PY) backend/scripts/create_demo_data.py

reset-demo:         ## remove demo data only
	$(PY) backend/scripts/create_demo_data.py --reset

backend:            ## FastAPI on 127.0.0.1:8000 (workers included)
	$(PY) -m uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000 --reload

frontend:           ## Next.js dashboard on http://localhost:3000
	cd frontend && $(NPM) run dev

test:               ## backend test suite (no credentials required)
	$(PY) -m pytest backend/tests -q

lint:
	$(PY) -m ruff check backend
	cd frontend && $(NPM) run lint

typecheck:
	$(PY) -m mypy
	cd frontend && $(NPM) run typecheck

eval:               ## scripted pipeline evaluation (Docker sandbox if available)
	$(PY) -m app.evaluation.evaluator --llm scripted --sandbox auto

eval-groq:          ## evaluation with the configured Groq model
	$(PY) -m app.evaluation.evaluator --llm groq --sandbox auto

openapi:            ## regenerate the OpenAPI schema and frontend TypeScript types
	$(PY) backend/scripts/export_openapi.py
	cd frontend && $(NPM) run gen:api

check-integrations:
	$(PY) backend/scripts/check_integrations.py

docker-up:          ## optional containerised dev stack (sandboxed execution disabled; see docs/SETUP.md)
	docker compose up --build
