# Agentic Commerce Gateway

A Shopify app that makes a merchant's catalog discoverable and purchasable by AI shopping agents.

| Path | Stack | Role |
|---|---|---|
| [`frontend-ui/`](frontend-ui) | React Router 7 (Shopify's Remix successor), Polaris, Prisma | Merchant OAuth, embedded admin dashboard, theme app extension |
| [`backend-engine/`](backend-engine) | Python 3.12, FastAPI, SQLModel, Alembic | Business logic, protocol adapters (UCP/ACP/MCP), webhooks, checkout handoff |
| `infra/`, `docker-compose*.yml` | Docker, Postgres 16 | Local stack and production smoke tests |

Both services share one Postgres database. Prisma owns the `shopify_app` schema (Shopify sessions only); Alembic owns the `gateway` schema (all domain tables). frontend-ui never reads `gateway` tables directly: it calls backend-engine with a 60-second Ed25519-signed service JWT. See [docs/architecture.md](docs/architecture.md) for the full blueprint. For the end-to-end flow from install to an agent sending a buyer to checkout, see [docs/flow.md](docs/flow.md).

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Monorepo, Docker, Railway config, Postgres schemas, service-JWT auth, installation checker | Done |
| 2 | Catalog auditor + AI Discovery Score, streaming `llm.txt`, dashboard | Done |
| 3 | UCP / ACP / MCP adapters, cart and inventory services | Done |
| 4 | Agent checkout handoff to the store's own Shopify checkout | Done |

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
   - `JWT_PUBLIC_KEY`, `BACKEND_JWT_PRIVATE_KEY`, `TOKEN_ENCRYPTION_KEY` (from `make keys`)
   - `FRONTEND_INTERNAL_URL=http://${{frontend-ui.RAILWAY_PRIVATE_DOMAIN}}:${{frontend-ui.PORT}}`
   - `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`
   - `PUBLIC_BASE_URL=https://<backend public domain>` (used in the UCP discovery profile)
   - Generate a public domain: agents and Shopify must reach `llm.txt`, protocol and webhook routes.
3. **frontend-ui**: New service from this repo, root directory `/frontend-ui`. Variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}?schema=shopify_app`
   - `BACKEND_INTERNAL_URL=http://${{backend-engine.RAILWAY_PRIVATE_DOMAIN}}:${{backend-engine.PORT}}`
   - `JWT_PRIVATE_KEY`, `BACKEND_JWT_PUBLIC_KEY` (from `make keys`)
   - `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SCOPES` (same as `shopify.app.toml`)
   - `SHOPIFY_APP_URL=https://<frontend public domain>`
   - `BACKEND_PUBLIC_URL=https://<backend public domain>` (shown to merchants on the Agent access page)
   - Generate a public domain, then set it as `application_url` and the auth redirect in `shopify.app.toml` and run `npm run deploy`.

Pushes to `main` redeploy only the service whose folder changed. Migrations run in each service's pre-deploy step (`alembic upgrade head`, `prisma migrate deploy`).

## AI Discovery Score

Each active product is scored 0-100 across eight weighted dimensions (identity 15, description 20, media 15, identifiers 15, offers 10, taxonomy 10, attributes 10, reviews 5). The store's score is the average. Meta-mapping rules (`/api/v1/merchant/mapping-rules`) let merchants credit facts encoded in tags or metafields, e.g. a `brand:(.+)` tag rule. See [`catalog_auditor.py`](backend-engine/app/services/catalog_auditor.py) for the exact rules.

## Agent feed: llm.txt

`GET /api/v1/agent-gateway/llm.txt?shop=<store>.myshopify.com` on backend-engine streams the store's policies, brand FAQs (`PUT /api/v1/merchant/faqs`), collections and active products as compact Markdown, cached for 10 minutes. To serve it at `https://<store>/llms.txt`, point a Shopify app proxy at backend-engine and add a URL redirect from `/llms.txt` to the proxy path.

## Agent checkout protocols

Merchants create an API key per agent on the **Agent access** page (one protocol per key, argon2-hashed, revocable). Agents send it as `Authorization: Bearer acg_live_…`.

| Protocol | Endpoints on backend-engine | Notes |
|---|---|---|
| ACP | `POST /api/v1/acp/checkout_sessions`, `GET`/`POST …/{id}`, `POST …/{id}/cancel` | [Agentic Checkout spec](https://developers.openai.com/commerce/specs/checkout); amounts in minor units |
| UCP | `POST /api/v1/ucp/checkout-sessions`, `GET`/`PUT …/{id}`, `POST …/{id}/cancel`; discovery at `/.well-known/ucp` | [UCP checkout REST binding](https://ucp.dev/specification/shopping/checkout/rest/) with the fulfillment extension |
| MCP | `POST /mcp` (streamable HTTP, stateless JSON) | Tools: `search_products`, `get_product`, `create_cart`, `update_cart`, `get_cart`, `cancel_cart` |

ACP and UCP writes require an `Idempotency-Key` header: a retry with the same key and body replays the stored response, and the same key with a different body gets `409`.

All three share [`cart_manager.py`](backend-engine/app/services/cart_manager.py): each change re-checks live stock (archived products, unsellable variants and tracked "deny" inventory; over-requests are clamped with a warning), then prices the cart through Shopify's `draftOrderCalculate`, so shipping rates and taxes come from the merchant's own settings. The cheapest rate is preselected. A cart is `ready` once it has sellable lines (and, when an address is given, the store ships there). The buyer's email and address are optional: they prefill checkout and make the quoted shipping and tax exact. Carts expire after 6 hours without changes.

## Checkout handoff

The gateway never takes payment or creates orders. Under [Shopify App Store rule 1.1.2](https://shopify.dev/docs/apps/launch/shopify-app-store/app-store-requirements), apps must not bypass Shopify checkout, so the buyer always pays on the merchant's own Shopify checkout:

1. A `ready` cart carries `checkout_url`, a [cart permalink](https://shopify.dev/docs/apps/build/checkout/create-cart-permalinks) such as `https://<store>/cart/<variant>:<qty>,…?checkout[email]=…&checkout[shipping_address][…]=…&attributes[_agentic_cart_id]=<cart id>`, with the lines, email and address prefilled.
2. ACP returns it as `continue_url`. UCP returns status `requires_escalation` with `continue_url` and a `requires_buyer_review` message. MCP's `create_cart`, `update_cart` and `get_cart` include `checkout_url`.
3. The agent hands the link to the buyer, who reviews and pays with Shopify Payments, Shop Pay or any gateway the store has. The order is a normal Shopify order, carrying the `_agentic_cart_id` cart attribute.
4. ACP and UCP `complete` endpoints answer `409 requires_escalation` with the link, and the UCP profile advertises no payment handlers.

Merchants see every agent cart (status, items, total, checkout link) on the **Agent checkouts** page. Linking carts to the orders they became needs `read_orders` and protected customer data approval, so it is left for later.

## Service-to-service auth

| Direction | Purpose | Key holder | Verifier |
|---|---|---|---|
| frontend-ui -> backend-engine | dashboard API calls, merchant sync | `JWT_PRIVATE_KEY` (frontend) | `JWT_PUBLIC_KEY` (backend) |
| backend-engine -> frontend-ui | fetch a fresh Shopify offline token (`POST /internal/admin-token`) | `BACKEND_JWT_PRIVATE_KEY` (backend) | `BACKEND_JWT_PUBLIC_KEY` (frontend) |

Shopify offline tokens now expire and are refreshed by frontend-ui's Shopify library, which owns the session. backend-engine never refreshes tokens itself, so the two services can't invalidate each other's refresh tokens.

## Running more than one replica

Both services are safe to scale horizontally. Everything that has to be shared lives in Postgres:

- **Service-JWT replay guard.** Each `jti` is stored until its token expires (`gateway.service_token_jtis` on backend-engine, `shopify_app.ServiceTokenJti` on frontend-ui), so a token is accepted once across all replicas.
- **llm.txt cache.** Rendered feeds live in `gateway.llm_txt_cache`, so an FAQ edit on one replica invalidates every copy.
- **Catalog audits.** `POST /api/v1/merchant/audits` queues a run. Every backend replica runs a worker that claims due runs with `FOR UPDATE SKIP LOCKED`, heartbeats while it works and retries transient failures with backoff (up to `AUDIT_MAX_ATTEMPTS`, default 3). If a process dies mid-audit, its heartbeat goes stale after `AUDIT_STALE_SECONDS` (default 120) and another worker restarts the run; writes from the abandoned attempt are rejected. On a graceful shutdown the run goes straight back to the queue. Set `AUDIT_WORKER_ENABLED=false` on replicas that should not run audits.

The remaining in-process caches are performance-only: verified agent keys (revocation is still read from the database on every request) and Shopify Admin tokens.

All database access in backend-engine is async (SQLAlchemy `AsyncSession` on psycopg 3), and CPU-heavy steps (argon2, storefront HTML parsing) run in worker threads, so a slow query or hash no longer stalls the event loop. Alembic still uses the synchronous psycopg2 driver.

## Repository layout

```
backend-engine/
  app/api/          FastAPI routers and auth dependencies
  app/protocols/    UCP / ACP adapters and the MCP server (wire format <-> cart_manager)
  app/services/     business logic: installation_checker, catalog_auditor, tag_mapper,
                    llm_txt_renderer, shopify_client, cart_manager, agent_keys
  app/jobs/         durable catalog-audit worker (Postgres job queue)
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
