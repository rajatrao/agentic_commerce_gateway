.PHONY: keys up up-ollama down logs test test-backend test-frontend lint prod-smoke

keys:            ## Once: generate the service keys into .env and frontend-ui/.env
	node scripts/generate-keys.mjs

up: keys         ## Start Postgres and backend-engine (then run `npm run dev` in frontend-ui/)
	docker compose up -d --build

up-ollama: keys  ## Same as `up`, plus Ollama running Qwen (qwen3:8b) for AI enrichment
	AI_PROVIDER=ollama docker compose --profile ollama up -d --build

down:
	docker compose --profile ollama down

logs:
	docker compose logs -f

test: test-backend test-frontend

test-backend:    ## pytest inside the backend container
	docker compose up -d postgres
	docker compose run --rm -e TEST_DATABASE_URL=postgresql://gateway:gateway@postgres:5432/gateway_test backend pytest

test-frontend:
	cd frontend-ui && npm test

lint:
	docker compose run --rm backend sh -c "ruff check . && ruff format --check . && mypy app"
	cd frontend-ui && npm run lint && npm run typecheck

prod-smoke:      ## Build and run the production images locally
	docker compose -f docker-compose.prod.yml up --build
