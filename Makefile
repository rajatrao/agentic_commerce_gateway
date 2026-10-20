.PHONY: keys up up-ollama down logs test test-backend test-frontend lint prod-smoke

keys:            ## Generate JWT + encryption keys into .env
	node scripts/generate-keys.mjs

up: keys         ## Start Postgres, backend-engine and frontend-ui with hot reload
	docker compose up --build

up-ollama: keys  ## Same as `up`, plus Ollama running Qwen (qwen3:8b) for AI enrichment
	AI_PROVIDER=ollama docker compose --profile ollama up --build

down:
	docker compose --profile ollama down

logs:
	docker compose logs -f

test: test-backend test-frontend

test-backend:    ## Runs against the compose Postgres (start it with `docker compose up -d postgres`)
	cd backend-engine && TEST_DATABASE_URL=postgresql://gateway:gateway@localhost:5432/gateway_test \
		uv run pytest

test-frontend:
	cd frontend-ui && npm test

lint:
	cd backend-engine && uv run ruff check . && uv run ruff format --check . && uv run mypy app
	cd frontend-ui && npm run lint && npm run typecheck

prod-smoke:      ## Build and run the production images locally
	docker compose -f docker-compose.prod.yml up --build
