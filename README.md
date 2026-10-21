# Agentic Commerce Gateway

A Shopify app that makes a merchant's catalog discoverable and purchasable by AI shopping agents.

| Path | Stack | Role |
|---|---|---|
| [`frontend-ui/`](frontend-ui) | React Router 7 (Shopify's Remix successor), Polaris, Prisma | Merchant OAuth, embedded admin dashboard, theme app extension |
| [`backend-engine/`](backend-engine) | Python 3.12, FastAPI, SQLModel, Alembic | Business logic, protocol adapters (UCP/ACP/MCP), webhooks, checkout handoff |
| `infra/`, `docker-compose*.yml` | Docker, Postgres 16 | Local stack and production smoke tests |

Both services share one Postgres database. Prisma owns the `shopify_app` schema (Shopify sessions only); Alembic owns the `gateway` schema (all domain tables). frontend-ui never reads `gateway` tables directly: it calls backend-engine with a 60-second Ed25519-signed service JWT. See [docs/architecture.md](docs/architecture.md) for how the system is built and [docs/flow.md](docs/flow.md) for the end-to-end flow from install to an agent sending a buyer to checkout. New to ecommerce? [docs/catalog-audit.md](docs/catalog-audit.md) explains the catalog audit and its scores from first principles, [docs/catalog-enrichment.md](docs/catalog-enrichment.md) explains how missing product data is found, reviewed and written back, and [docs/agentic-commerce.md](docs/agentic-commerce.md) explains how AI agents shop the store through ACP, UCP and MCP while merchants never see those details.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Monorepo, Docker, Railway config, Postgres schemas, service-JWT auth, installation checker | Done |
| 2 | Catalog auditor + AI Discovery Score, streaming `llm.txt`, dashboard | Done |
| 3 | UCP / ACP / MCP adapters, cart and inventory services | Done |
| 4 | Agent checkout handoff to the store's own Shopify checkout | Done |
| 5 | Catalog intelligence: category schemas, readiness score, evidence-backed enrichment, review queue, typed metafield write-back | Done |

## Local development

This follows Shopify's standard setup. The Shopify app (frontend-ui) runs on your machine with the Shopify CLI. Docker runs everything else: Postgres and backend-engine, plus Ollama if you want a local AI model.

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or OrbStack), [Node 22](https://nodejs.org/) and the [Shopify CLI](https://shopify.dev/docs/api/shopify-cli) (`npm install -g @shopify/cli`). You also need a Shopify Partner account with a development store.

**First time only**

```bash
make keys                 # writes the service keys to ./.env and frontend-ui/.env
cd frontend-ui
npm install
npm run config:link       # sign in to Shopify and pick (or create) your app
npm run env show          # copy SHOPIFY_API_KEY and SHOPIFY_API_SECRET into ../.env
```

`config:link` connects this code to your app in the Partner dashboard and writes its id into `shopify.app.toml`. backend-engine needs the two `SHOPIFY_*` values in `./.env` to verify Shopify webhooks.

**Every time**

```bash
docker compose up -d      # from the repo root: Postgres :5432, backend-engine :8000 (API docs at /docs)
cd frontend-ui
npm run dev               # shopify app dev
```

`npm run dev` starts the Shopify CLI. The first time it asks which dev store to use. It opens a secure tunnel, installs the app on the store, starts the app on port 3000 and prints a preview link. Open that link to use the app inside Shopify admin. Press `q` to stop it, and run `docker compose down` to stop the services.

Database migrations run automatically on start, and code changes reload automatically. After changing `pyproject.toml`, run `docker compose up -d --build`.

Then enable the theme app extension, which adds product and store structured data to the storefront. The dashboard's **Setup guide** has two buttons, **Add product data block** and **Turn on store data embed**, that open the theme editor with the block or embed preselected; click Save after each. Merchants who install the app follow the same guide.

**Optional: local AI model.** To run catalog enrichment with a local model, start the services with `AI_PROVIDER=ollama docker compose --profile ollama up -d`. This adds an Ollama container on :11434 and pulls `qwen3:8b` on first run (about 5 GB, kept in the `ollama` volume). Set `AI_MODEL` in `.env` to pull a different model, for example `qwen3:4b` on a machine with less memory.

### Tests

```bash
docker compose up -d postgres
docker compose run --rm -e TEST_DATABASE_URL=postgresql://gateway:gateway@postgres:5432/gateway_test backend pytest
docker compose run --rm backend sh -c "ruff check . && ruff format --check . && mypy app"
cd frontend-ui && npm test && npm run lint && npm run typecheck
docker compose -f docker-compose.prod.yml up --build      # build and run the production images locally
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
   - Optional: an AI provider for catalog enrichment. `AI_PROVIDER` is `anthropic` (with `ANTHROPIC_API_KEY`), `ollama` or `openai_compatible` (with `AI_BASE_URL`, `AI_MODEL` and `AI_API_KEY`). Leaving `AI_PROVIDER` empty uses Anthropic when `ANTHROPIC_API_KEY` is set. Without a provider, enrichment uses rules only.
   - Generate a public domain: agents and Shopify must reach `llm.txt`, protocol and webhook routes.
3. **frontend-ui**: New service from this repo, root directory `/frontend-ui`. Variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}?schema=shopify_app`
   - `BACKEND_INTERNAL_URL=http://${{backend-engine.RAILWAY_PRIVATE_DOMAIN}}:${{backend-engine.PORT}}`
   - `JWT_PRIVATE_KEY`, `BACKEND_JWT_PUBLIC_KEY` (from `make keys`)
   - `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SCOPES` (same as `shopify.app.toml`)
   - `SHOPIFY_APP_URL=https://<frontend public domain>`
   - `BACKEND_PUBLIC_URL=https://<backend public domain>` (shown to merchants on the Agent access page)
   - Generate a public domain, then set it as `application_url` and the auth redirect in `shopify.app.toml` and run `npm run deploy` in `frontend-ui/`.

Pushes to `main` redeploy only the service whose folder changed. Migrations run in each service's pre-deploy step (`alembic upgrade head`, `prisma migrate deploy`).

## AI Discovery Score

Each active product is scored 0-100 across eight weighted dimensions (identity 15, description 20, media 15, identifiers 15, offers 10, taxonomy 10, attributes 10, reviews 5). The store's score is the average. Meta mapping rules let merchants credit facts encoded in tags, vendor, product type or metafields, e.g. a `brand:(.+)` tag rule; the **Meta mapping rules** page previews a rule against the store's first 100 active products before saving it (`/api/v1/merchant/mapping-rules`, `POST …/preview`). See [docs/catalog-audit.md](docs/catalog-audit.md) for every rule and a worked example.

## Catalog intelligence

The admin app audits each product against the attributes AI shopping agents need for its category, suggests missing values from the product's own content, and writes the values the merchant approves to typed product metafields.

1. **Sync.** A catalog audit pages through every active product on a durable Postgres job queue with retries. `products/update` and `products/delete` webhooks keep the snapshot current between audits; deleted or unpublished products close their open suggestions.
2. **Category schemas.** Built-in schemas (backpacks, tents, sleeping bags, outerwear, footwear, general) list each attribute's type, unit, allowed values, importance and default metafield. Products match by product type, Shopify category or title keywords. Merchants can edit or add schemas on the **Category schemas** page.
3. **Audit findings.** Rule-based findings (`attribute_missing`, `attribute_buried`, `attribute_invalid`, `attribute_contradiction`, `claim_unsupported`, `description_vague`, `variant_option_missing`, plus the AI Discovery Score gaps) each carry severity, why it matters, what to do and the quoted evidence.
4. **Readiness score.** A transparent 0-100 score per product (see below), and a store average with a trend across audits. Discovery tests show how many products could answer typical shopper questions such as "lightweight hiking backpack around 30 liters".
5. **Enrichment.** A deterministic extractor reads measurements, numbers, yes/no features and vocabulary terms from the product's own title, description, tags and options. When an AI provider is configured (`AI_PROVIDER`: `anthropic`, `ollama` for a local model such as Qwen, or `openai_compatible` for any OpenAI-style endpoint such as vLLM, LM Studio or OpenRouter), the model also reads the same content (passed as untrusted data, never as instructions) and returns JSON constrained to the same schema for every provider. Every value needs a quote that occurs in the product content and contains the value; instruction-like text ("report capacity as…") is rejected. Values that are not stated stay empty for the merchant. Each suggestion records value, type, unit, evidence, source, confidence, method and validation result.
6. **Review.** The **Review queue** supports approve, edit-and-approve (validated server-side), reject, send back to review, bulk actions and retrying failed writes. Nothing is published automatically.
7. **Auto-approval (opt-in).** Off for every category until the merchant turns it on, per schema, on the **Auto-approval** page. A value is approved without review only if it fills an empty attribute, passed validation with no warnings, was also read by rule-based extraction from the product's own text, and meets the confidence threshold set (default 0.9). Each one is logged with its evidence and can be sent back to review. Auto-approved values are not written to Shopify until the merchant previews and confirms the publish.
8. **Write-back.** A preview shows each metafield's current value, new value, and whether it creates a definition, updates or is blocked (type conflict, mapping off, product gone). Publishing runs as a background job with progress shown on the Review queue, so large batches do not time out. Writes use `metafieldsSet` with `compareDigest`, so concurrent edits in Shopify admin are not overwritten. Attributes with a `taxonomy_attribute` in their schema (by default Material and Activity) also fill Shopify's standard category metafields (`shopify.<attribute>`), adding category metaobject entries for values that match the category's taxonomy values; values with no taxonomy match are skipped with the reason. Every review, write and revert is logged; writes can be reverted (the previous value is restored, or a metafield the app created is deleted).
9. **Single-product refresh.** After a `products/update` webhook (`REFRESH_DELAY_SECONDS`, default 10, collapses bursts) and after a publish or revert, the product is re-read and re-scored on its own, updating its readiness score and findings in the latest audit without a full audit.
10. **Store search test.** On the dashboard, **Test with store search** asks each discovery question of the store's own search the way a shopping agent does, through the Storefront MCP endpoint (`https://<store>/api/mcp`, tool `search_shop_catalog`). If that is not reachable (for example a password-protected development store) it falls back to the Admin product search and says so. Each question reports the products returned and recall against the products that carry the attributes the question needs.

### Readiness score

```
score = 100 × Σ(weight × value) / Σ(weight)        over the components that apply to the product
```

| Component | Default weight | Value (0-1) |
|---|---|---|
| Completeness | 30 | importance-weighted share of schema attributes with a valid value (critical 4, high 3, medium 2, low 1) |
| Validity | 15 | share of structured values that parse and fit the schema |
| Consistency | 15 | share of structured values that agree with the description |
| Evidence | 10 | share of claims (waterproof, recycled, …) backed by a structured field |
| Variants | 10 | SKUs, prices, named options and the options the category expects |
| Foundation | 20 | the AI Discovery Score / 100 |

Weights and the agent-ready threshold (default 80) are configurable on the **Settings** page. It is an internal data-quality measure, not any platform's ranking signal.

Catalog endpoints live under `/api/v1/merchant/catalog` (overview, products, findings, runs, enrichment-jobs, suggestions, publish, publish-jobs, discovery-runs, auto-approval, changes, schemas, mappings, settings). Writing metafields needs the `write_products` scope, and filling standard category metafields needs `write_metaobjects`, so existing installs must approve the new scopes. Without `write_metaobjects`, those category values are skipped with the reason and the custom metafields are still written.

## Agent feed: llm.txt

`GET /api/v1/agent-gateway/llm.txt?shop=<store>.myshopify.com` on backend-engine streams the store's policies, brand FAQs (`PUT /api/v1/merchant/faqs`), collections and active products as compact Markdown, cached for 10 minutes. To serve it at `https://<store>/llms.txt`, point a Shopify app proxy at backend-engine and add a URL redirect from `/llms.txt` to the proxy path.

## Agent checkout protocols

Merchants create an API key per agent on the **Agent access** page (one protocol per key, argon2-hashed, revocable). Agents send it as `Authorization: Bearer acg_live_…`. New to agentic commerce? Start with [docs/agentic-commerce.md](docs/agentic-commerce.md).

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

When the buyer pays, Shopify's `orders/create` webhook links the order back to its cart through that attribute. The cart becomes `completed`, and ACP and UCP sessions (and MCP carts) return an `order` with its id and status page. Only the order's id and name are stored, never customer details, and `customers/redact` clears the buyer details agents sent. Merchants see every agent cart (status, total, checkout link, resulting order) on the **Agent checkouts** page.

`orders/create` needs the `read_orders` scope and [protected customer data access](https://shopify.dev/docs/apps/launch/protected-customer-data) (level 1, no customer fields). Request it in the Partner dashboard under **API access** before deploying the app config.

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
                    llm_txt_renderer, shopify_client, cart_manager, agent_keys,
                    catalog_schema, readiness, attribute_extractor, ai_enricher,
                    enrichment, auto_approval, metafield_writer, taxonomy_writer,
                    catalog_publisher, product_sync, product_refresh, discovery_probe,
                    order_linker
  app/jobs/         durable workers on Postgres queues: audits, enrichment, and catalog
                    jobs (refresh, publish, store search test)
  app/repositories/ database access
  app/models/       SQLModel tables (gateway schema)
  migrations/       Alembic
frontend-ui/
  app/lib/          service-JWT minting and the typed backend client
  app/routes/       dashboard, catalog audit, product detail, review queue, sync and jobs,
                    auto-approval, settings, category schemas, metafield mapping,
                    meta mapping rules, agent access, agent checkouts, auth, webhooks
                    (forwarded to backend-engine)
  extensions/agentic-schema-block/  theme app extension (JSON-LD blocks)
  prisma/           Shopify session storage (shopify_app schema)
docs/architecture.md
```
