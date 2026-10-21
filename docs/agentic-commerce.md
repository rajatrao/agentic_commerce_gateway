# Agentic commerce: how the app serves AI agents

This guide is for readers who are new to agentic commerce. It explains what the term means, what the three agent protocols (ACP, UCP and MCP) are, how the app speaks each one, and how it keeps all of that out of the merchant's way. Every endpoint and field named here comes from the code under `backend-engine/app/` (`protocols/`, `api/v1/acp.py`, `api/v1/ucp.py`, `api/mcp.py`, `services/cart_manager.py`).

Related guides: [catalog-audit.md](catalog-audit.md) and [catalog-enrichment.md](catalog-enrichment.md) cover how product data is made agent-ready. [architecture.md](architecture.md) covers the whole system.

---

## 1. What "agentic commerce" means

Until recently, online shopping meant a person clicking through a store's website. In **agentic commerce**, the person tells an AI assistant what they want ("a waterproof jacket under $300 in size M, delivered to Austin"), and the assistant does the legwork. It searches stores, compares products, checks stock and prices, builds a cart, and brings the person to the point of paying.

The assistant doing this work is an **AI shopping agent**. Examples are ChatGPT, Gemini, Claude, or a company's own shopping bot. An agent can't click around a website the way a person does, at least not reliably. It needs:

1. **A way to read the catalog**: products, variants, prices and stock, in a form a program understands.
2. **A way to act**: create a cart, change it, and get a real price including shipping and tax.
3. **A safe handoff to payment**: the shopper, not the agent, should confirm and pay.

Each AI platform has published its own standard for (2) and (3). These standards are the **protocols**. A store that wants to be shoppable by every agent would otherwise have to implement all of them. This app implements them once, for every Shopify store that installs it.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Agent** | An AI system acting for a shopper. |
| **Protocol** | An agreed set of web addresses (endpoints) and message formats that agents and stores use to talk. Like a common language. |
| **Endpoint** | One web address the app answers, such as `POST /api/v1/acp/checkout_sessions`. |
| **Checkout session / cart** | The agent's shopping basket: the items, quantities, shopper details, shipping choice and totals. ACP and UCP call it a *checkout session*, MCP calls it a *cart*. Inside the app they are the same `agent_carts` row. |
| **Variant id** | The id of the exact buyable item (the blue jacket in size M). Agents put variant ids and quantities in a cart. |
| **API key** | A secret string an agent sends with every request to prove the merchant allowed it. The app's keys look like `acg_live_…`. |
| **Idempotency key** | A unique id the agent attaches to each change. If the network drops and the agent retries, the app recognises the retry and returns the first answer instead of doing the change twice. |
| **Checkout handoff** | Instead of the agent paying, the app gives it a link to the store's own Shopify checkout, where the shopper reviews and pays. |
| **Minor units** | Money as a whole number of the smallest unit: $24.99 is `2499`. ACP and UCP use this so amounts never have rounding errors. |

## 3. The three protocols at a glance

| | **ACP** | **UCP** | **MCP** |
|---|---|---|---|
| Full name | Agentic Commerce Protocol | Universal Commerce Protocol | Model Context Protocol |
| Backed by | OpenAI (with Stripe); used by ChatGPT | Google and partners; used by Gemini and Google surfaces | Anthropic's open standard for AI tools; used by Claude and many other assistants |
| Style | REST: the agent calls web addresses with JSON bodies | REST, plus a discovery profile that says what the store supports | Tools: the agent calls named functions (`search_products`, `create_cart`) over one endpoint |
| Main object | checkout session | checkout session | cart |
| Money format | integers in minor units, lowercase currency (`usd`) | integers in minor units, uppercase currency (`USD`) | decimal strings (`"24.99"`) |
| Spec version implemented | `2026-09-29` | `2026-08-25` (checkout + fulfillment capabilities) | MCP streamable HTTP, stateless JSON |
| Discovery | the merchant gives the platform the endpoint | `GET /.well-known/ucp` | the agent lists the server's tools |
| Ready to pay looks like | `status: ready_for_payment` + `continue_url` | `status: requires_escalation` + `continue_url` | `status: ready` + `checkout_url` |

All three end the same way: the shopper gets a link to the store's own Shopify checkout. The app never accepts payment details.

---

## 4. Shared groundwork

### 4.1 How an agent finds the store

Agents learn about the store through several public, read-only sources. None of them needs an API key:

| Source | Address | What it gives the agent |
|---|---|---|
| Storefront structured data | every product page and the homepage | JSON-LD that describes the store (Organization, WebSite with a search action) and each product (offers, prices, availability, rating). The theme app extension adds it. |
| `llm.txt` feed | `GET /api/v1/agent-gateway/llm.txt?shop=<store>.myshopify.com` | The whole store as compact Markdown: summary, policies, brand FAQs, collections, and every active product with its **variant ids**, options, prices and stock. This is often the fastest way for an agent to learn what to put in a cart. Cached for 10 minutes. |
| UCP profile | `GET /.well-known/ucp` | The protocol version, capabilities (`dev.ucp.shopping.checkout`, `dev.ucp.shopping.fulfillment`), the REST endpoint (`/api/v1/ucp`) and the MCP endpoint (`/mcp`), and an empty `payment_handlers` object, which tells agents that payment happens elsewhere. |

The better the catalog data (the audit and enrichment features), the more useful these sources are to an agent.

### 4.2 How an agent proves it is allowed

The merchant creates one **API key per agent** on the **Agent access** page and picks its protocol. The key is shown once, stored only as an argon2 hash, and can be revoked at any time. Each key works for exactly one protocol: an ACP key is refused on UCP or MCP routes.

The agent sends the key with every request as `Authorization: Bearer acg_live_…` (or `X-API-Key`). Keys carry scopes: `catalog:read` for MCP's product tools and `checkout` for carts. New keys get both.

### 4.3 One cart engine behind all three protocols

Each protocol has an **adapter** (`protocols/acp_adapter.py`, `ucp_adapter.py`, `mcp_server.py`) that only translates. It turns the protocol's request into one neutral set of cart changes (items, buyer, address, shipping choice) and turns the resulting cart back into the protocol's response shape. The real work happens once, in `services/cart_manager.py`:

1. **Check stock live** for each line, from Shopify. Archived products and variants that can't be sold are removed with a message. A request for more than the tracked stock is reduced to what's available, with a warning.
2. **Price the cart** with Shopify's `draftOrderCalculate`. That uses the store's own shipping rates, taxes and discounts, so the agent quotes the shopper what Shopify will actually charge. No draft order is saved.
3. **Preselect the cheapest shipping option**, which the agent can change.
4. **Decide the status.** The cart is `open` until it has at least one sellable line and, if an address was given, the store ships there. Then it becomes `ready` and gets a `checkout_url`. Carts expire after 6 hours without changes.
5. **Explain anything missing** in `messages` (codes such as `missing`, `out_of_stock`, `item_unavailable`), with a path pointing at the field concerned. Agents are told to read the messages on every response.

The shopper's email and address are optional. They make shipping and tax exact and prefill the checkout, but a cart can be ready without them.

### 4.4 Retries are safe

ACP and UCP calls that change a cart need an `Idempotency-Key` header. The app stores each response for 24 hours:

- Same key, same body: the stored response is returned with `Idempotent-Replayed: true`, and nothing changes twice.
- Same key, different body: `409`, because that's a bug in the agent.
- Same key while the first request is still running: `409`.
- A server error releases the key so the agent can retry.

### 4.5 The checkout handoff

When a cart is `ready`, it carries `checkout_url`, a Shopify **cart permalink** such as:

```
https://<store>/cart/44012345678:1,44012345999:2?checkout[email]=…&checkout[shipping_address][city]=…&attributes[_agentic_cart_id]=<cart id>
```

Opening it takes the shopper straight to the store's normal Shopify checkout, with the items, email and address filled in. They pay there with whatever the store accepts (Shopify Payments, Shop Pay, PayPal and so on). The order is an ordinary Shopify order. The hidden `_agentic_cart_id` attribute lets the app link the order back to the agent's cart. (That link uses the `orders/create` webhook, which is turned off until the app is granted Shopify's protected customer data access; see [architecture.md](architecture.md#12-current-caveats).)

The app works this way on purpose:

- **Shopify's rules.** App Store rule 1.1.2 says apps must not bypass Shopify checkout or payments.
- **Trust.** The shopper sees the real store, the real total and the real policies before paying.
- **No money risk.** The app never stores card details, never charges, and never issues refunds.

If an agent calls a protocol's `complete` endpoint anyway, it gets `409 requires_escalation` and the link.

---

## 5. ACP (Agentic Commerce Protocol)

ACP is OpenAI's checkout standard, which ChatGPT uses to shop merchants. The app implements the cart-building part of the [Agentic Checkout spec](https://developers.openai.com/commerce/specs/checkout) but leaves out delegated payment.

| Call | Endpoint | Notes |
|---|---|---|
| Create | `POST /api/v1/acp/checkout_sessions` | `items` (variant `id` + `quantity`), optional `buyer`, optional `fulfillment_address`. Returns `201`. |
| Read | `GET /api/v1/acp/checkout_sessions/{id}` | |
| Update | `POST /api/v1/acp/checkout_sessions/{id}` | Any of `items` (replaces all), `buyer`, `fulfillment_address`, `fulfillment_option_id` (choose shipping). |
| Cancel | `POST /api/v1/acp/checkout_sessions/{id}/cancel` | |
| Complete | `POST /api/v1/acp/checkout_sessions/{id}/complete` | Not offered: `409 requires_escalation` with the link. |

**Request:**

```json
POST /api/v1/acp/checkout_sessions
Authorization: Bearer acg_live_…
Idempotency-Key: 7f6c…

{
  "items": [{ "id": "44012345678", "quantity": 1 }],
  "buyer": { "email": "sam@example.com" },
  "fulfillment_address": {
    "name": "Sam Lee", "line_one": "1 Main St", "city": "Austin",
    "state": "TX", "country": "US", "postal_code": "78701"
  }
}
```

**Response (shortened):**

```json
{
  "id": "5b0e…",
  "status": "ready_for_payment",
  "currency": "usd",
  "line_items": [{ "id": "…", "item": { "id": "44012345678", "quantity": 1 },
                   "subtotal": 24900, "tax": 2054, "total": 26954 }],
  "fulfillment_options": [{ "type": "shipping", "id": "…", "title": "Standard", "total": 800 }],
  "fulfillment_option_id": "…",
  "totals": [
    { "type": "subtotal", "amount": 24900 }, { "type": "fulfillment", "amount": 800 },
    { "type": "tax", "amount": 2054 },       { "type": "total", "amount": 27754 }
  ],
  "messages": [{ "type": "info", "param": "$.continue_url",
                 "content": "The buyer pays on the store's own checkout: send them to continue_url." }],
  "continue_url": "https://<store>/cart/44012345678:1?…"
}
```

Status mapping: `open` → `not_ready_for_payment`, `ready` → `ready_for_payment`, `completed` → `completed`, canceled or expired → `canceled`. After the order is linked, the session also returns `order` with its id and status page. Errors use ACP's own shape and codes (`missing`, `invalid`, `out_of_stock`).

## 6. UCP (Universal Commerce Protocol)

UCP is the standard Google and its partners use. A store publishes a **profile** that says which capabilities it supports, then exposes checkout sessions over REST (the [REST binding](https://ucp.dev/specification/shopping/checkout/rest/)). The app supports the checkout capability and the fulfillment extension (shipping).

| Call | Endpoint | Notes |
|---|---|---|
| Discover | `GET /.well-known/ucp` | Public. Endpoints, capabilities, no payment handlers. |
| Create | `POST /api/v1/ucp/checkout-sessions` | `line_items` (`item.id` + `quantity`), optional `buyer`, optional `fulfillment` with a destination address. |
| Read | `GET /api/v1/ucp/checkout-sessions/{id}` | |
| Update | `PUT /api/v1/ucp/checkout-sessions/{id}` | The spec treats updates as full replacements; the app keeps fields the agent leaves out (the lenient reading). Choose shipping with `selected_option_id` in the fulfillment group. |
| Cancel | `POST /api/v1/ucp/checkout-sessions/{id}/cancel` | |
| Complete | `POST /api/v1/ucp/checkout-sessions/{id}/complete` | Not offered: `409 requires_escalation`. |

Every response carries a `ucp` envelope with the version and capabilities. Addresses use UCP's names (`street_address`, `address_locality`, `address_region`, `postal_code`, `address_country`). Shipping options appear under `fulfillment.methods[].groups[].options`, each with its total.

Status mapping: `open` → `incomplete`, `ready` → **`requires_escalation`**, plus a message with code `requires_buyer_review` and the `continue_url`. In UCP, "requires escalation" means a human has to take the next step, which here is the shopper paying on the store's checkout.

## 7. MCP (Model Context Protocol)

MCP is an open standard that lets any AI assistant use **tools**: named functions with typed inputs, described so the model knows when to call them. Instead of learning a REST API, the assistant connects to `POST /mcp`, reads the tool list and the server's instructions, and calls tools as the conversation needs them.

The app's MCP server ("Store checkout") runs in stateless JSON streamable-HTTP mode, so any backend replica can answer any call. It offers six tools:

| Tool | Scope | What it does |
|---|---|---|
| `search_products(query, limit≤25)` | `catalog:read` | Searches the store's published, active products and returns variants with ids, options, prices and availability. |
| `get_product(product_id)` | `catalog:read` | Full details for one product. |
| `create_cart(lines, email?, shipping_address?)` | `checkout` | Creates a cart: stock checked, priced with real shipping and tax, cheapest shipping preselected. |
| `update_cart(cart_id, lines?, email?, shipping_address?, shipping_option_id?)` | `checkout` | Changes a cart. `lines` replaces all lines, and omitted fields are kept. |
| `get_cart(cart_id)` | `checkout` | Current state. |
| `cancel_cart(cart_id)` | `checkout` | Abandons the cart. |

The server's instructions tell the model, in plain words, to search, build a cart with variant ids, read `messages` every time, add the email and address for an exact quote, and give the shopper `checkout_url` when the status is `ready`. It also says the model never handles payment. MCP is also the only protocol where the agent can search the catalog through the app. ACP and UCP agents read the catalog from `llm.txt` or the storefront.

---

## 8. An example agent journey

A shopper asks an assistant: *"Find me a lightweight daypack around 28 liters for hiking, and get it to me in Austin."*

1. **Discover.** The assistant reads `/.well-known/ucp` (UCP agents) or the `llm.txt` feed, or calls `search_products("hiking daypack")` (MCP agents). Thanks to catalog enrichment, the Trailhead 28L Daypack lists capacity 28 L, weight 850 g and intended use "hiking", so the agent can tell it fits. It notes variant `44012345678` (Blue).
2. **Create the cart.** It sends the variant and quantity 1, with the shopper's email and Austin address, using its protocol's create call and a fresh idempotency key.
3. **The app does the work.** It checks the Blue variant is in stock, asks Shopify for the price with real shipping and Texas sales tax, and preselects Standard shipping. Everything needed is there, so the cart is `ready` with a checkout link.
4. **Present the offer.** The agent tells the shopper: "$249.00 plus $8.00 shipping and $20.54 tax, $277.54 total. Here's the store's checkout to confirm." If the shopper wants express shipping, the agent updates the cart with that option, and the totals and link refresh.
5. **Handoff.** The shopper taps the link, lands on the store's own Shopify checkout with everything filled in, and pays.
6. **Afterwards.** The merchant sees the cart on **Agent checkouts** and fulfils the Shopify order as usual. Once order linking is on, the cart shows **Ordered** and the agent can show the shopper the order status page.

If the network drops at step 2 and the agent retries with the same idempotency key, it gets the same cart back, not a second one. If the Blue variant sells out between steps 2 and 4, the next update or read returns an `out_of_stock` message on that line and the agent can offer Green instead.

---

## 9. What the merchant never has to deal with

Merchants are shopkeepers, not protocol engineers. The app's job is to make agent shopping something they switch on, not something they build.

| What agentic commerce needs | Without the app | With the app |
|---|---|---|
| Speak three protocols | Implement and keep up with ACP, UCP and MCP specs, each with its own versions, field names, money formats, statuses and error shapes | Nothing. The adapters translate; spec updates are a backend change. |
| Machine-readable catalog | Hand-write JSON-LD, build a feed, fix data gaps | Turn on two theme blocks; `llm.txt` is generated; the audit and enrichment fill the gaps from the merchant's own text with approval. |
| Accurate prices | Reimplement shipping rules, tax rules and discounts for agents | Shopify calculates every cart with the store's existing settings. |
| Stock accuracy | Sync inventory to each platform | Checked live on every cart change. |
| Agent access control | Build authentication, key issuing and revocation | One click per agent on **Agent access**; revoke any time; keys are hashed. |
| Duplicate or retried requests | Build idempotency | Built in for ACP and UCP. |
| Payments | Integrate each platform's payment scheme, handle card data, fraud and refunds | None. The shopper pays on the store's existing checkout, with its existing payment providers, fraud checks and refund flow. |
| Orders | A second order pipeline | Ordinary Shopify orders, fulfilled the usual way. |
| Compliance | Work out whether agent payments break platform rules | The handoff design keeps the app within Shopify App Store rules, and no customer details are stored beyond what agents send for the cart (cleared on a GDPR request). |

What the merchant actually does:

1. Install the app and approve its permissions.
2. On the dashboard's **Setup guide**, click the two buttons that turn on the product and store data blocks, and run the installation check.
3. Run a catalog audit, review and publish the suggested values, and watch the readiness score rise.
4. On **Agent access**, create a key for each AI platform, choose its protocol, and send the platform the key and the gateway's public URL, both shown on the page.
5. Watch **Agent checkouts**, and fulfil orders in Shopify as normal.

Everything else in this guide happens without them.

## 10. Where to look in the code

| Concern | File |
|---|---|
| Shared adapter contract, errors, handoff answer | `backend-engine/app/protocols/base.py` |
| ACP request and response shapes | `backend-engine/app/protocols/acp_adapter.py`, routes in `app/api/v1/acp.py` |
| UCP shapes, envelope, discovery profile | `backend-engine/app/protocols/ucp_adapter.py`, routes in `app/api/v1/ucp.py` |
| MCP tools and instructions | `backend-engine/app/protocols/mcp_server.py`, mount and key check in `app/api/mcp.py` |
| Key check, idempotency, error mapping | `backend-engine/app/api/agent.py` |
| Stock, pricing, status, checkout link | `backend-engine/app/services/cart_manager.py` |
| Agent keys | `backend-engine/app/services/agent_keys.py`, page `frontend-ui/app/routes/app.agents.tsx` |
| Order linking | `backend-engine/app/services/order_linker.py` |
| `llm.txt` | `backend-engine/app/services/llm_txt_renderer.py`, route `app/api/v1/agent_gateway.py` |
