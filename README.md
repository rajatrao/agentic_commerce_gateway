# Agentic Commerce Gateway

A Shopify app that makes a merchant's catalog discoverable and purchasable by AI shopping agents.

| Path | Stack | Role |
|---|---|---|
| [`frontend-ui/`](frontend-ui) | React Router 7 (Shopify's Remix successor), Polaris, Prisma | Merchant OAuth, embedded admin dashboard, theme app extension |
| [`backend-engine/`](backend-engine) | Python 3.12, FastAPI, SQLModel, Alembic | Business logic, protocol adapters (UCP/ACP/MCP), webhooks, payments |
| `infra/`, `docker-compose*.yml` | Docker, Postgres 16 | Local stack and production smoke tests |

Both services share one Postgres database. Prisma owns the `shopify_app` schema (Shopify sessions only); Alembic owns the `gateway` schema (all domain tables). frontend-ui never reads `gateway` tables directly: it calls backend-engine with a 60-second Ed25519-signed service JWT. See [docs/architecture.md](docs/architecture.md) for the full blueprint.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Monorepo, Docker, Railway config, Postgres schemas, service-JWT auth, installation checker | Done |
| 2 | Catalog auditor + AI Discovery Score, streaming `llm.txt`, dashboard | Next |
| 3 | UCP / ACP / MCP adapters, cart and inventory services | Planned |
| 4 | Delegated-token agent checkout, paid draft orders | Planned |

## Local development

Prerequisites: Docker Desktop (or OrbStack), Node 22, [uv](https://docs.astral.sh/uv/), and the [Shopify CLI](https://shopify.dev/docs/api/shopify-cli) logged into your Partner account.

```bash
make keys                 # writes JWT + encryption keys to ./.env (gitignored)
docker compose up --build # Postgres :5432, backend-engine :8000 (/docs), frontend-ui :3000
```

To open the app inside Shopify admin, run the Shopify CLI from `frontend-ui/` on your machine (it creates the tunnel and handles Partner login):

```bash
cd frontend-ui
npm install
npm run config:link       # first time only: links shopify.app.toml to your Partner app
cp .env.example .env      # paste JWT_PRIVATE_KEY from ../.env
docker compose up -d postgres backend   # from the repo root
npm run dev               # shopify app dev
```

`shopify app dev` installs the app on your dev store. The `afterAuth` hook registers the shop with backend-engine, after which the dashboard's **Run installation check** button works.

Then enable the theme app extension: Online Store > Themes > Customize, add the **Agentic Product Schema** block to the product template and turn on the **Agentic Store Schema** app embed.

### Tests

```bash
docker compose up -d postgres
make test        # backend: pytest against Postgres; frontend: vitest
make lint        # ruff, mypy, eslint, tsc
make prod-smoke  # build and run the production images locally
```

## Deploying to Railway

One Railway project, three services. Each app service has a `railway.json` (config as code), so builds, health checks and migrations are already defined.

1. **Postgres**: add the Railway Postgres database.
2. **backend-engine**: New service from this GitHub repo, root directory `/backend-engine`. Variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - `HOST=::` (Railway's private network is IPv6)
   - `JWT_PUBLIC_KEY`, `TOKEN_ENCRYPTION_KEY` (from `make keys`)
   - `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`
   - Generate a public domain: agents and Shopify must reach `llm.txt`, protocol and webhook routes.
3. **frontend-ui**: New service from this repo, root directory `/frontend-ui`. Variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}?schema=shopify_app`
   - `BACKEND_INTERNAL_URL=http://${{backend-engine.RAILWAY_PRIVATE_DOMAIN}}:${{backend-engine.PORT}}`
   - `JWT_PRIVATE_KEY` (from `make keys`)
   - `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SCOPES` (same as `shopify.app.toml`)
   - `SHOPIFY_APP_URL=https://<frontend public domain>`
   - Generate a public domain, then set it as `application_url` and the auth redirect in `shopify.app.toml` and run `npm run deploy`.

Pushes to `main` redeploy only the service whose folder changed. Migrations run in each service's pre-deploy step (`alembic upgrade head`, `prisma migrate deploy`).

## Repository layout

```
backend-engine/
  app/api/          FastAPI routers and auth dependencies
  app/protocols/    UCP / ACP / MCP translation layer (Phase 3)
  app/services/     pure business logic (installation_checker.py, ...)
  app/repositories/ database access
  app/models/       SQLModel tables (gateway schema)
  migrations/       Alembic
frontend-ui/
  app/lib/          service-JWT minting and the typed backend client
  app/routes/       dashboard, auth, webhooks (forwarded to backend-engine)
  extensions/agentic-schema-block/  theme app extension (JSON-LD blocks)
  prisma/           Shopify session storage (shopify_app schema)
docs/architecture.md
```
