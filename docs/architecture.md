# Agentic Commerce Gateway: Plan Review Blueprint

Status: approved 2026-10-15. Phase 1 implemented.

## 0. Environment check (read-only)

These checks ran in the sandbox shell that can see your project folder. It's a Linux VM, not your Mac's own toolchain, so your Mac may have different versions.

| Tool | Found | Needed | Plan |
|---|---|---|---|
| Node | v22.23.2 | ≥20.19 | OK |
| npm | 10.9.8 | any | OK |
| Python | 3.10.12 only | 3.11+ | `uv` 0.12 is present, so `uv python install 3.12` and a uv-managed venv |
| PostgreSQL | not found | 16 | `docker-compose.yml` with Postgres 16 for local dev (or a Neon/Supabase URL via `DATABASE_URL`) |
| Docker | not found | for local PG | Needed on the machine that runs the stack, or use a hosted PG |
| Shopify CLI | not found | for `shopify app init/dev` | Must run on your Mac, logged into your Partner account with a dev store |
| git | 2.34 | any | OK. Repo is empty, remote is `rajatrao/agentic_commerce_gateway` |

Two notes on the brief:
- **Remix to React Router 7.** Shopify's current `shopify app init` template is React Router 7, which is what Remix v3 became. It's the same loaders/actions model. **Default:** use the current React Router template, which is still called "the Remix app" below. The legacy Remix template can be pinned instead if you prefer.
- **`shopify-api-python`** is published on PyPI as **`ShopifyAPI`** (repo `Shopify/shopify_python_api`). We'll use that package, plus `httpx` for raw GraphQL Admin calls.

## 1. Monorepo folder structure

```
agentic_commerce_gateway/
├── README.md
├── docker-compose.yml            # local dev: postgres + backend + frontend (hot reload)
├── docker-compose.prod.yml       # local smoke test of the production images
├── .dockerignore
├── .env.example                  # shared var names; each app has its own .env
├── .github/workflows/ci.yml      # lint + test both stacks
├── docs/
│   ├── architecture.md
│   └── auth.md
├── frontend-ui/                  # Shopify embedded app (TypeScript)
│   ├── Dockerfile                # multi-stage: deps → build → slim runtime
│   ├── railway.json              # Railway config-as-code for this service
│   ├── shopify.app.toml
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── prisma/
│   │   ├── schema.prisma         # provider = "postgresql", schema "shopify_app"
│   │   └── migrations/
│   ├── app/
│   │   ├── shopify.server.ts     # shopifyApp() + PrismaSessionStorage + afterAuth hook
│   │   ├── db.server.ts          # Prisma client singleton
│   │   ├── lib/
│   │   │   ├── backend.server.ts # typed FastAPI client (attaches internal JWT)
│   │   │   ├── jwt.server.ts     # mints short-lived service JWTs (jose)
│   │   │   └── types.ts          # DTOs mirrored from FastAPI OpenAPI
│   │   ├── routes/
│   │   │   ├── app.tsx                       # Polaris AppProvider + nav
│   │   │   ├── app._index.tsx                # Dashboard: AI Discovery Score
│   │   │   ├── app.installation.tsx          # Installation checker results
│   │   │   ├── app.products.tsx              # Products needing structured-data fixes
│   │   │   ├── app.mappings.tsx              # Meta-mapping rules CRUD
│   │   │   ├── app.checkouts.tsx             # Agent carts + Shopify checkout links
│   │   │   ├── app.settings.tsx              # Protocol toggles, agent API keys
│   │   │   ├── auth.$.tsx / auth.login/
│   │   │   └── webhooks.app.uninstalled.tsx  # Thin: forwards to backend
│   │   └── components/           # ScoreGauge, CheckList, ProductIssueTable...
│   └── extensions/
│       └── agentic-schema-block/ # Theme app extension: JSON-LD app block
│           ├── blocks/product-jsonld.liquid
│           └── shopify.extension.toml
└── backend-engine/               # FastAPI (Python 3.12 via uv)
    ├── Dockerfile                # multi-stage: uv builder → python:3.12-slim runtime
    ├── railway.json              # Railway config-as-code for this service
    ├── pyproject.toml            # uv-managed; requirements.txt exported from it
    ├── requirements.txt
    ├── alembic.ini
    ├── migrations/               # Alembic owns the "gateway" schema
    │   └── versions/
    ├── app/
    │   ├── main.py               # app factory, routers, lifespan (DB pool, MCP mount)
    │   ├── core/
    │   │   ├── config.py         # pydantic-settings
    │   │   ├── db.py             # SQLModel engine, pool sizing, session dependency
    │   │   ├── security.py       # JWT verify, HMAC verify, token encryption (Fernet)
    │   │   └── logging.py        # structlog + PII/token redaction
    │   ├── models/               # SQLModel tables (one file per aggregate)
    │   │   ├── merchant.py
    │   │   ├── audit.py
    │   │   ├── mapping.py
    │   │   └── transaction.py        # agent carts (no payment records)
    │   ├── schemas/              # Pydantic request/response DTOs (no ORM)
    │   ├── repositories/         # DB access only; services never touch the Session directly
    │   ├── services/             # Pure business logic, no FastAPI imports
    │   │   ├── installation_checker.py   # Phase 1
    │   │   ├── catalog_auditor.py        # Phase 2: AI Discovery Score
    │   │   ├── llm_txt_renderer.py       # Phase 2: streaming generator
    │   │   ├── tag_mapper.py             # Applies meta-mapping rules
    │   │   ├── inventory_tracker.py
    │   │   ├── cart_manager.py           # Phase 3
    │   │   └── shopify_client.py         # GraphQL Admin API wrapper (httpx + retries)
    │   ├── protocols/            # Translation layer: wire format <-> domain models
    │   │   ├── base.py           # ProtocolAdapter ABC + canonical CheckoutIntent
    │   │   ├── ucp_adapter.py
    │   │   ├── acp_adapter.py
    │   │   └── mcp_server.py     # async MCP server (official `mcp` SDK, streamable HTTP)
    │   └── api/
    │       ├── deps.py           # auth dependencies: merchant_jwt, agent_client, shopify_hmac
    │       └── v1/
    │           ├── router.py
    │           ├── merchant.py       # /api/v1/merchant/* (dashboard, called by Remix)
    │           ├── agent_gateway.py  # GET /api/v1/agent-gateway/llm.txt (public, streamed)
    │           ├── ucp.py            # UCP endpoints -> ucp_adapter -> services
    │           ├── acp.py            # ACP checkout endpoints -> acp_adapter -> services
    │           ├── agent_checkout.py # POST /api/v1/agent-checkout
    │           ├── webhooks.py       # Shopify webhooks (HMAC-verified)
    │           └── internal.py       # /internal/merchants/sync (afterAuth token sync)
    └── tests/
        ├── unit/                 # services + adapters, no DB
        ├── integration/          # testcontainers Postgres
        └── fixtures/             # sample product payloads, ACP/UCP requests
```

Layering rule: `api` → `protocols` → `services` → `repositories` → `models`. Nothing below `api` imports FastAPI, and `services` never imports a protocol adapter.

## 2. PostgreSQL schema

One database with **two Postgres schemas**, so each stack owns its own migrations and they never fight:

- `shopify_app` is owned by **Prisma**. It holds only the Shopify session storage table the template requires.
- `gateway` is owned by **Alembic/SQLModel**. It holds all domain tables. Remix never queries these; it reads them through FastAPI.

The offline Admin token gets to the backend through the Remix `afterAuth` hook, which POSTs it to `/internal/merchants/sync`. It is stored Fernet-encrypted in `gateway.merchants`.

### `shopify_app."Session"` (Prisma, from the Shopify template)
`id` PK, `shop`, `state`, `isOnline`, `scope`, `expires`, `accessToken`, `userId`, `firstName`, `lastName`, `email`, `accountOwner`, `locale`, `collaborator`, `emailVerified`

### `gateway` schema (SQLModel)

**merchants**: Merchant session tracking
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| shop_domain | text UNIQUE | `x.myshopify.com` |
| access_token_enc | bytea | Fernet-encrypted offline token |
| scopes | text | |
| status | enum(`active`,`uninstalled`) | |
| installed_at / uninstalled_at | timestamptz | |
| settings | jsonb | protocol toggles, llm.txt options |

**installation_checks**
| id uuid PK | merchant_id FK | store_url text | jsonld_valid bool | schemas_found text[] | has_product_schema bool | has_org_schema bool | app_block_enabled bool | findings jsonb | checked_at timestamptz |

**audit_runs**
| id uuid PK | merchant_id FK | started_at | finished_at | products_scanned int | avg_score numeric(5,2) | avg_readiness numeric(5,2) | stats jsonb (schema counts, discovery tests) | status enum |

**product_audits**: Product AI audits
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| run_id | FK audit_runs | |
| merchant_id | FK | |
| product_gid | text | `gid://shopify/Product/…` |
| title | text | |
| score | smallint 0–100 | AI Discovery Score |
| breakdown | jsonb | per-dimension points (identifiers, description quality, media alt text, variants/GTIN, price/availability, reviews, metafields) |
| missing_fields | text[] | drives the "needs updates" list |
| content_hash | text | skips re-scoring unchanged products |
| audited_at | timestamptz | |
| | | index `(merchant_id, product_gid, audited_at DESC)` |

**meta_mapping_rules**
| id uuid PK | merchant_id FK | source_type enum(`tag`,`metafield`,`product_type`,`vendor`) | source_key text | match_pattern text (regex/glob) | target_property text (schema.org / UCP field) | transform jsonb | priority int | active bool |

**agent_clients**: registered AI agents/platforms allowed to call protocol endpoints
| id uuid PK | merchant_id FK | protocol enum(`ucp`,`acp`,`mcp`) | name | key_prefix text | key_hash text (argon2) | scopes text[] | created_at | revoked_at |

**agent_carts**
| id uuid PK | merchant_id FK | protocol | agent_client_id FK | external_session_id text | lines jsonb | buyer jsonb | shipping_address jsonb | shipping_options jsonb | shipping_rate_handle text | messages jsonb | subtotal/shipping/tax/total numeric(12,2) | currency char(3) | checkout_url text (Shopify cart permalink, set while `ready`) | order_gid, order_name, order_status_url text, ordered_at (set by orders/create) | status enum(`open`,`ready`,`completed`,`expired`,`canceled`) | expires_at | created_at | updated_at |

**idempotency_records**: stored responses for agent `Idempotency-Key` replays (24 h)
| id bigint PK | agent_client_id FK | key text | request_hash text | status_code int (NULL while in flight) | response_body jsonb | created_at | UNIQUE `(agent_client_id, key)` |

**catalog_products**: latest snapshot of each product, kept current by audits and products/update|delete webhooks
| id uuid PK | merchant_id FK | product_gid text | title, product_type, vendor, status | category_name | schema_key | attributes jsonb (current structured values by attribute) | readiness_score smallint | discovery_score smallint | shopify_updated_at | synced_at | removed_at | UNIQUE `(merchant_id, product_gid)` |

**audit_findings**: one row per issue (rule-based per audit run, or AI per enrichment job)
| id bigint PK | merchant_id FK | run_id FK audit_runs (cascade) | job_id FK enrichment_jobs (cascade) | product_gid | code | attribute_key | severity enum(`critical`,`high`,`medium`,`low`) | method `rule`/`ai` | message | why_it_matters | recommended_action | evidence | confidence numeric(3,2) |

**category_schemas**: merchant overrides or additions to the built-in category schemas (jsonb definition)

**metafield_mappings**: per-attribute write target (namespace, key, metafield type, enabled), overriding schema defaults

**enrichment_jobs**: durable queue (same claim/heartbeat/fencing scheme as audit_runs)
| id uuid PK | merchant_id FK | status run_status | product_gids text[] | use_ai bool | total/succeeded/failed int | results jsonb (per product) | attempts | next_attempt_at | heartbeat_at |

**attribute_suggestions**: proposed attribute values with provenance
| id uuid PK | merchant_id FK | job_id FK | product_gid | schema_key | attribute_key | data_type | current_value / proposed_value / approved_value jsonb | evidence text | evidence_source | confidence | method (`rule_extraction`, `llm:<model>`, `+merchant_edit`) | validation_status, validation_errors | rule_confirmed bool (rule extraction read the same value) | status enum(`pending`,`approved`,`rejected`,`superseded`,`published`,`failed`,`reverted`) | reviewed_at / published_at | partial UNIQUE `(merchant_id, product_gid, attribute_key)` while pending or approved |

**catalog_jobs**: durable queue for background publishing, single-product refreshes and store search tests (same claim/heartbeat/fencing scheme); one worker takes refreshes, another publish and discovery jobs
| id uuid PK | merchant_id FK | kind enum(`publish`,`refresh`,`discovery`) | status run_status | dedupe_key (product gid for refreshes; a queued job with the same key is reused) | source | params jsonb | total/processed/succeeded/failed int | results jsonb | attempts | next_attempt_at | heartbeat_at | finished_at |

**catalog_changes**: append-only audit log of reviews, auto-approvals, reopens, writes and reverts (before/after jsonb, details, reverted_at)

**webhook_events**: dedupe and replay
| id bigserial | shop_domain | topic | webhook_id text UNIQUE | payload jsonb | received_at | processed_at |

## 3. Authentication between stacks

There are four trust boundaries, each with its own mechanism:

1. **Merchant browser → Remix.** This is standard Shopify embedded-app auth. App Bridge issues a session token (ID token), and `authenticate.admin(request)` validates it with token exchange. The browser **never** calls FastAPI directly.

2. **Remix server → FastAPI (service JWT).**
   - In each loader/action, after `authenticate.admin`, `backend.server.ts` mints a JWT with `jose`:
     `iss=frontend-ui`, `aud=backend-engine`, `sub=<shop_domain>`, `scope=["merchant:read","merchant:write"]`, `iat`, `exp=iat+60s`, `jti=uuid`.
   - It's signed **EdDSA (Ed25519)**. Remix holds the private key and FastAPI holds only the public key, so a compromised backend can't forge UI calls.
   - It's sent as `Authorization: Bearer <jwt>` over TLS on the private network.
   - The FastAPI `merchant_jwt` dependency verifies the signature, `aud`, `iss` and `exp` (with 10s leeway), then rejects the request unless `sub` matches an `active` row in `merchants`. Every query is scoped by that merchant id, which gives tenant isolation.
   - `jti` replay guard: stored in Postgres (`gateway.service_token_jtis` in FastAPI, `shopify_app.ServiceTokenJti` in Remix) until the token expires, so a token is accepted once across every replica.
   - `/internal/*` (token sync) also requires `scope=internal:sync`.

2b. **FastAPI → Remix (fresh Shopify tokens, added in Phase 2).** Shopify offline tokens now expire and are refreshed by the Shopify library in Remix, which owns the session. FastAPI therefore asks Remix for a valid token with `POST /internal/admin-token`, authenticated by a second Ed25519 pair (`BACKEND_JWT_PRIVATE_KEY` in FastAPI, `BACKEND_JWT_PUBLIC_KEY` in Remix), and caches it until five minutes before expiry. The token synced at install is only a fallback.

3. **Shopify → FastAPI webhooks.** These are verified with `X-Shopify-Hmac-Sha256` (HMAC-SHA256 of the raw body using the app secret) and deduped by `X-Shopify-Webhook-Id`. That includes `app/uninstalled` and the mandatory GDPR topics.

4. **AI agents → FastAPI protocol endpoints.**
   - `llm.txt` is public, with caching and rate limits.
   - ACP and UCP use a per-merchant agent API key (`Authorization: Bearer acg_live_…`, argon2-hashed in `agent_clients`), plus a required `Idempotency-Key` header. Request-signature verification is added where the protocol spec defines it.
   - MCP uses the same key, or OAuth 2.1 later per the MCP auth spec.
   - **No payment credentials.** The gateway accepts none. ACP/UCP `complete` returns `409 requires_escalation` with `continue_url`, and the UCP profile lists no payment handlers.

**Checkout handoff (Phase 4):** every cart change re-checks stock and re-prices through `draftOrderCalculate` (no draft order is created). A `ready` cart gets `checkout_url`, a Shopify cart permalink (`https://<shop>/cart/<variant>:<qty>,…`) that prefills email and shipping address and carries the cart id as the `_agentic_cart_id` cart attribute. The agent hands that link to the buyer, who pays on the merchant's own Shopify checkout. That keeps the app within App Store rule 1.1.2 (no bypassing Shopify checkout or payments). The `orders/create` webhook (`read_orders`, protected customer data level 1) reads that attribute and marks the cart `completed` with `order_gid`, `order_name`, `order_status_url` and `ordered_at`; the webhook log keeps only the order's identifiers. `customers/redact` clears buyer details on that customer's carts.

## 4. Dependencies

### `frontend-ui/package.json` (template defaults plus additions)
- Shopify app: `@shopify/shopify-app-react-router` (or `@shopify/shopify-app-remix` if pinned), `@shopify/app-bridge-react`, `@shopify/polaris`, `@shopify/polaris-icons`
- Data: `@prisma/client`, `@shopify/shopify-app-session-storage-prisma`, **`pg`**
- Auth/validation: `jose`, `zod`
- Dev: `prisma`, `typescript`, `vite`, `@shopify/cli`, `eslint`, `prettier`, `vitest`

### `backend-engine/requirements.txt`
- API: `fastapi`, `uvicorn[standard]`, `pydantic-settings`
- DB: **`sqlmodel`**, **`psycopg2-binary`**, `alembic`
- Shopify: **`ShopifyAPI`** (the "shopify-api-python" package), `httpx`
- Auth/crypto: `pyjwt[crypto]`, `cryptography`, `argon2-cffi`
- Catalog/installation checks: `extruct` (JSON-LD/microdata), `beautifulsoup4`, `lxml`
- Protocols: `mcp` (official MCP Python SDK)
- Resilience/ops: `tenacity`, `structlog`
- Dev: `pytest`, `pytest-asyncio`, `testcontainers[postgres]`, `ruff`, `mypy`

On the DB driver: psycopg2 is sync-only. With it, DB calls run in FastAPI's threadpool (`pool_size=10, max_overflow=20, pool_pre_ping=True`), which is fine at this scale. If you want fully async DB access, swap it for `psycopg[binary]` (v3).

## 5. Docker: local development and Railway production

### Images (one Dockerfile per service, used for both local and Railway)
**`backend-engine/Dockerfile`**
- Builder: `python:3.12-slim` with `uv` (pip-installed, so builds only need Docker Hub and PyPI), `uv sync --frozen --no-dev` into `/app/.venv`.
- Runtime: `python:3.12-slim`, non-root user, copies only `.venv` + `app/` + `migrations/` + `alembic.ini`.
- `CMD uvicorn app.main:app --host $HOST --port $PORT --proxy-headers`. `HOST` defaults to `0.0.0.0`; set `HOST=::` on Railway because its private network is IPv6.
- A `HEALTHCHECK` hits `/health`.

**`frontend-ui/Dockerfile`**
- Stages: `node:22-alpine` deps (`npm ci`) → build (`prisma generate && npm run build`) → runtime with prod deps only, running as a non-root user.
- `CMD npm run start`, which listens on `$PORT`.

### Local development: `docker-compose.yml`
| service | image | notes |
|---|---|---|
| `postgres` | `postgres:16-alpine` | named volume, healthcheck, creates `shopify_app` + `gateway` schemas via init SQL |
| `backend` | built from `backend-engine/Dockerfile` (`dev` target) | bind-mounts `app/`, `uvicorn --reload`, runs `alembic upgrade head` on start, port 8000 |
| `frontend` | built from `frontend-ui/Dockerfile` (`dev` target) | bind-mounts source, `prisma migrate deploy` then `shopify app dev`-compatible server, port 3000 |

- Usage: `docker compose up` starts everything.
- `shopify app dev` (the tunnel and Partner login) still runs on your Mac. It points at the containerised frontend, or you run the frontend natively and only Postgres and the backend in Docker; both work.
- `docker-compose.prod.yml` builds the production targets so you can smoke-test exactly what Railway will run.
- You'll need Docker Desktop (or OrbStack) on your Mac. The sandbox has no Docker, so I'll write the files, and you'll run the first build.

### Railway production (one project, three services)
| Railway service | Source | Config |
|---|---|---|
| **Postgres** | Railway Postgres plugin | provides `DATABASE_URL` |
| **backend-engine** | GitHub repo, root dir `backend-engine/` | `railway.json`: Dockerfile builder, `preDeployCommand: alembic upgrade head`, `healthcheckPath: /health`, `watchPatterns: ["backend-engine/**"]`, restart on failure |
| **frontend-ui** | GitHub repo, root dir `frontend-ui/` | `railway.json`: Dockerfile builder, `preDeployCommand: npx prisma migrate deploy`, `healthcheckPath: /healthz`, `watchPatterns: ["frontend-ui/**"]` |

- **Variables:**
  - Both services use the reference variable `DATABASE_URL=${{Postgres.DATABASE_URL}}`. Prisma appends `?schema=shopify_app`, and SQLModel sets `search_path=gateway`.
  - The frontend reaches the backend at `BACKEND_INTERNAL_URL=http://${{backend-engine.RAILWAY_PRIVATE_DOMAIN}}:${{backend-engine.PORT}}` over Railway's private network, so dashboard traffic never leaves Railway.
  - Secrets (`SHOPIFY_API_KEY/SECRET`, `JWT_PRIVATE_KEY` frontend only, `JWT_PUBLIC_KEY` backend only, `TOKEN_ENCRYPTION_KEY`) go in Railway service variables, never in images.
- **Public domains:**
  - The frontend gets a domain, which becomes `application_url` in `shopify.app.toml`.
  - The backend also gets a public domain, because `llm.txt`, the UCP/ACP/MCP endpoints and Shopify webhooks must be reachable by agents and Shopify. Its `/api/v1/merchant/*` and `/internal/*` routes still require the service JWT, so being public doesn't open them.
- **Deploys:** pushes to `main` auto-deploy only the service whose `watchPatterns` changed. Optionally, a PR environment on Railway for previews.
- **CI** (`.github/workflows/ci.yml`): lint, test and `docker build` both images on every PR, so a broken Dockerfile never reaches Railway.

## 6. Execution roadmap (after approval)

| Phase | Deliverable | Done when |
|---|---|---|
| 1 | Monorepo, Dockerfiles + `docker-compose` (PG, backend, frontend) + Railway `railway.json` configs, Remix app with Prisma on PG, FastAPI skeleton with pool/Alembic, service-JWT handshake, `installation_checker.py` | `/health` green; Remix dashboard calls FastAPI with a JWT; checker returns JSON-LD/app-block findings for a live URL |
| 2 | `catalog_auditor.py` + scoring, streaming `llm.txt`, Polaris dashboard (score, checks, fix list) | Score persisted per product; `llm.txt` streams |
| 3 | `protocols/` adapters (UCP, ACP, MCP server), `cart_manager.py` with GraphQL inventory/shipping/tax | Agent can create and price a cart via each protocol |
| 4 | Checkout handoff: `checkout_url` cart permalinks, ACP `continue_url`, UCP `requires_escalation`, Agent checkouts page | A ready cart's link opens the store's Shopify checkout with the lines, email and address prefilled |

**Where it runs:** I can write all files directly into your folder. `shopify app init`/`dev` needs Shopify CLI logged into your Partner account, so that step runs on your Mac. I'd generate the template there and fold it into `frontend-ui/`.
