# Project flow

End to end: merchant install, catalog readiness (installation check and the durable audit queue), agent API keys, agent discovery, agent carts handed off to the store's Shopify checkout, and merchant follow-up. See [architecture.md](architecture.md) for the design behind each step.

![Project flow](flow.png)

The diagram source is below; GitHub renders it too. To re-render the PNG: `npx -p @mermaid-js/mermaid-cli mmdc -i flow.mmd -o flow.png -s 2 -b white` after copying the block into `flow.mmd`.

```mermaid
flowchart TD
  %% ================= 1. Onboarding =================
  subgraph S1["1 · Merchant onboarding"]
    direction TB
    A1(["Merchant installs the app<br/>from Shopify admin"]) --> A2["frontend-ui: Shopify OAuth<br/>session saved via Prisma"]
    A2 --> A3["afterAuth hook<br/>POST /internal/merchants/sync<br/>service JWT"]
    A3 --> A4[("merchants row<br/>offline token Fernet-encrypted")]
    A4 --> A5["Merchant enables theme blocks<br/>Product + Store JSON-LD"]
  end

  %% ================= 2. Readiness =================
  subgraph S2["2 · Get the catalog agent-ready"]
    direction TB
    B1["Dashboard: Run installation check"] --> B2["Fetch storefront, product page,<br/>robots.txt, llms.txt<br/>SSRF-guarded"]
    B2 --> B3{"JSON-LD, metadata,<br/>app block, crawlers OK?"}
    B3 -- "No" --> B4["Findings + fixes<br/>shown to merchant"]
    B3 -- "Yes" --> B5["Check passed"]
    B6["Dashboard: Run audit"] --> B7[("audit_runs: queued")]
    B7 --> B8["Audit worker claims run<br/>SKIP LOCKED + heartbeat"]
    B8 --> B9["Page products from<br/>Shopify Admin GraphQL"]
    B9 --> B10["Score each product 0-100<br/>8 dimensions + mapping rules"]
    B10 --> B11{"Attempt OK?"}
    B11 -- "Transient error" --> B7
    B11 -- "Done" --> B12["AI Discovery Score,<br/>top issues, worst products"]
    B12 --> B13["Merchant fixes products,<br/>adds mapping rules and FAQs"]
    B13 --> B6
  end

  %% ================= 3. Agent access =================
  subgraph S3["3 · Give agents access"]
    direction TB
    C1["Agent access page:<br/>create API key per agent<br/>ACP, UCP or MCP"] --> C2[("agent_clients<br/>argon2 hash, shown once")]
  end

  %% ================= 4. Discovery =================
  subgraph S4["4 · Agent discovers the store"]
    direction TB
    D1(["AI shopping agent"]) --> D2["Reads storefront JSON-LD"]
    D1 --> D3["GET llm.txt<br/>catalog as Markdown"]
    D1 --> D4["GET /.well-known/ucp<br/>endpoints, no payment handlers"]
    D3 --> D5{"Cached in<br/>Postgres?"}
    D5 -- "Yes" --> D6["Serve cached feed"]
    D5 -- "No" --> D7["Stream from Shopify,<br/>then cache 10 min"]
  end

  %% ================= 5. Checkout =================
  subgraph S5["5 · Agent builds a cart, buyer checks out"]
    direction TB
    E1["Create checkout<br/>ACP, UCP or MCP create_cart<br/>API key + Idempotency-Key"] --> E2["Check live stock<br/>price via draftOrderCalculate"]
    E2 --> E3{"Sellable lines? Ships to<br/>the address, if given?"}
    E3 -- "Missing" --> E4["Cart open + messages<br/>email and address optional"]
    E4 --> E2
    E3 -- "Complete" --> E5["Cart ready"]
    E5 --> E6["checkout_url: Shopify cart permalink<br/>lines, email, address prefilled"]
    E6 --> E7["Agent sends buyer the link<br/>ACP continue_url, UCP requires_escalation"]
    E7 --> E8["Buyer pays on the store's<br/>own Shopify checkout"]
    E8 --> E9(["Normal Shopify order<br/>cart attribute _agentic_cart_id"])
  end

  %% ================= 6. After the order =================
  subgraph S6["6 · Merchant follows up"]
    direction TB
    F1["Agent checkouts page:<br/>every agent cart + checkout link"] --> F2["Fulfil in Shopify as usual"]
    F3["Uninstall or GDPR webhooks<br/>HMAC verified, de-duplicated"] --> F4["Merchant marked uninstalled,<br/>token wiped, keys stop working"]
  end

  A5 --> B1
  A5 --> B6
  B5 --> C1
  B12 --> C1
  C2 --> D1
  D6 --> E1
  D7 --> E1
  D2 --> E1
  D4 --> E1
  E6 --> F1
  E9 --> F2
```
