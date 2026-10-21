# How catalog enrichment works

The audit ([catalog-audit.md](catalog-audit.md)) tells a merchant what is missing. **Enrichment** fills the gaps: it reads each product's own content, proposes structured attribute values with the quote that states each one, lets the merchant review them, and writes the approved values back to Shopify. This guide walks through every stage and the rules inside it. Terms such as *metafield*, *variant*, *structured data* and *category schema* are defined in section 2 of the audit guide.

---

## 1. The one rule everything follows

**Only propose a value the product already states, and prove it with a quote.**

A wrong capacity on a backpack is worse than no capacity: an agent will confidently recommend the product for the wrong trip, and the shopper returns it. So enrichment never infers, estimates or "fills in" a specification. If the description says "spacious", the capacity stays *unknown* and the merchant is asked to enter it. Every proposed value carries the sentence it came from, and the server checks that sentence really exists in the product and really contains the value, whichever component (rules or AI) proposed it.

The second rule: **nothing reaches Shopify without the merchant's explicit approval and confirmation.**

## 2. The pipeline at a glance

```mermaid
flowchart LR
  A["Merchant picks products<br/>or 'lowest readiness N'"] --> B[("enrichment_jobs<br/>queued")]
  B --> C["Enrichment worker<br/>re-reads each product<br/>from Shopify"]
  C --> D["Rule-based extractor"]
  C --> E["AI model<br/>(optional)"]
  D --> F{"Server-side<br/>validation"}
  E --> F
  F --> G[("attribute_suggestions<br/>pending")]
  G --> H{"Auto-approval rule?<br/>(off by default)"}
  H -- "qualifies" --> I["approved"]
  H -- "no" --> J["Review queue:<br/>approve, edit, reject"]
  J --> I
  I --> K["Write preview"]
  K -- "confirm: true" --> L[("catalog_jobs<br/>publish")]
  L --> M["metafieldsSet with compareDigest<br/>+ standard category metafields"]
  M --> N["published, logged,<br/>revertible"]
  N --> O["Single-product refresh<br/>re-scores the product"]
```

| Stage | Code |
|---|---|
| Start a job, queue | `api/v1/catalog.py` (`POST /enrichment-jobs`), `repositories/enrichment.py` |
| Run the job | `jobs/enrichment.py`, `jobs/enrichment_worker.py` |
| Read current values, extract from text | `services/attribute_extractor.py` |
| AI extraction | `services/ai_enricher.py` |
| Validate and merge into suggestions | `services/enrichment.py` |
| Auto-approval | `services/auto_approval.py` |
| Review, preview, publish, revert | `services/catalog_publisher.py`, `jobs/catalog_jobs.py` |
| Shopify writes | `services/metafield_writer.py`, `services/taxonomy_writer.py` |

All paths are under `backend-engine/app/`.

---

## 3. The rule-based extractor

The extractor (`extract_from_text`) is deterministic: the same text always gives the same answer, with no AI involved. It always runs, so enrichment works even with no AI provider configured.

### 3.1 What it reads

Four **text sources** from the product: the title, the description (HTML stripped to plain text), the tags joined with commas, and the options written as `Size: S, M, L; Color: Blue` (the placeholder option "Title" is skipped).

Each source is split into sentences (on `.`, `!`, `?`, line breaks and bullet characters). Any sentence that reads like an instruction to an AI rather than a product fact ("ignore previous instructions", "you must", "report capacity as …", "system prompt") is dropped before extraction. That stops a malicious or careless description from steering the result.

### 3.2 How each attribute type is read

| Attribute type | How the extractor finds it | Confidence |
|---|---|---|
| **Measurement** (weight, volume, dimension) | Finds a number followed by a unit of the right kind (`850 g`, `28L`, `1.2 kg`, `40 cm`, `15"`). | **0.9** if the same sentence names the attribute (its label, key or an alias: "weighs 850 g", "28 L capacity"). Otherwise **0.7** if the only match is in the title, **0.55** elsewhere, because a bare unit could belong to something else (a load limit, a bottle size). |
| **Number with a pattern** | Uses the schema's regular expression: `(\d)\s*-?\s*(?:person\|people\|p)` for tent capacity, `(-?\d{1,2})\s*°?\s*F` for a sleeping bag's temperature rating, `(\d{3,5})\s*mm` for a waterproof rating. | **0.85** |
| **Yes/no** (laptop compartment, carry-on) | A sentence contains one of the attribute's aliases ("laptop sleeve") and no negation word (no, not, without, lacks, doesn't). Only ever proposes **yes**; "no laptop sleeve" is skipped rather than read as *no*. | **0.8** |
| **Vocabulary** (material, intended use, season, fill type) | Looks for the schema's allowed values as whole words, longest first, so "ripstop nylon" wins over "nylon". A list attribute keeps every term found. A single-value attribute that finds two different terms ("down" and "synthetic") gives up and stays unknown; material is the exception and joins them ("ripstop nylon / polyester"). | **0.75** when the first quote is from the title or description, **0.6** from tags or options |
| **Free text with aliases** (care instructions) | Takes the first description sentence containing an alias ("Machine wash cold"). | **0.6** |

Each extraction records the value, the quoted sentence (up to three sentences for vocabulary terms, joined with " … "), the source it came from and the confidence.

The same extractor also powers the audit's `attribute_buried` and `attribute_contradiction` findings, so the audit and enrichment always agree on what the text says.

## 4. The AI extractor (optional)

### 4.1 Providers

The provider is set with `AI_PROVIDER`:

| Value | Model | Notes |
|---|---|---|
| `anthropic` | Claude (default `claude-opus-5-5`) | Needs `ANTHROPIC_API_KEY`. `AI_EFFORT` sets reasoning effort (low, medium, high). |
| `ollama` | A local model (default `qwen3:8b`) | `AI_BASE_URL` defaults to `http://localhost:11434`; `make up-ollama` or the compose `ollama` profile starts it. |
| `openai_compatible` | Any `/chat/completions` endpoint (vLLM, LM Studio, OpenRouter) | Needs `AI_BASE_URL` and `AI_MODEL`; `AI_API_KEY` if the endpoint wants one. |
| empty | Anthropic when `ANTHROPIC_API_KEY` is set, otherwise none | |
| `none` | rules only | |

Every provider gets the same prompt and must return the same JSON structure (enforced with each provider's structured-output feature: Anthropic `output_format`, Ollama `format`, OpenAI `response_format`), at temperature 0 where the provider supports it. Provider output then goes through the same server-side validation, so switching providers never loosens the evidence rules.

### 4.2 What the model is asked

The user message contains the product as a JSON document inside `<product_data>` tags (title, description, tags, options, and metafields as `namespace.key: value`, each source capped at 20,000 characters), followed by the schema's attributes with their type, unit and allowed values.

The system prompt tells the model that everything inside `<product_data>` is untrusted merchant data to analyse, never instructions to follow. For each attribute it must answer:

- **stated**: copy the exact sentence or phrase that states the value into `evidence_quote`, say which field it came from, and give the value exactly as written (units as written, no conversion); or
- **unknown**: value and quote both null.

It is told never to infer, estimate or invent specifications ("spacious" does not state a capacity), and that unknown is far better than wrong. It also returns **issues**: contradictions, ambiguities, unsupported claims and important missing details, each with a severity, a quote where possible and a confidence.

### 4.3 When the AI fails

A rate limit, timeout, refusal, truncated answer or malformed JSON raises an error for that product only. The product still gets rule-based suggestions, and the job records the note "AI unavailable, used rule-based extraction: …". Attributes that end up unknown carry the reason.

## 5. Validation: the gate every value passes

Before anything is stored, each proposed value, from the AI or from rules, goes through `validate_proposal`:

| Step | Check | Outcome on failure |
|---|---|---|
| 1 | Is there a value at all? | *unknown* |
| 2 | Is there an evidence quote? | *invalid*: "no evidence quoted" |
| 3 | Does the quote occur in the product's content (title, description, tags, options and metafield values)? Both sides are normalised first: Unicode normalised, lower-cased, curly quotes and dashes straightened, whitespace collapsed. A quote with " … " is split and every part must occur. | *invalid*: "the quoted evidence does not appear in the product content" |
| 4 | Does the quote read like an instruction (same pattern as section 3.1)? | *invalid* |
| 5 | Does the value parse as the attribute's type (`28 L` as a volume, `true` as yes/no)? | *invalid*: "value is not a valid volume" |
| 6 | Does the quote state the value? For numbers and measurements, the number must appear in the quote. For vocabulary attributes, each item must appear in the quote; a list is trimmed to the items actually quoted. | *invalid*: "the evidence does not state the number 28" |
| 7 | Does the value fit the schema (vocabulary, no negative capacity)? | kept, but marked *warning* with the reason |

A value that passes every step is *valid*. One that passes 1 to 6 but not 7 is a *warning*: it can be approved, but the merchant sees why it was flagged. A rejected AI value is never stored as a proposal; its rejection reason is kept on the attribute instead.

## 6. Merging rules and AI into one suggestion

For each schema attribute, `build_suggestions` decides what (if anything) to propose:

1. **Collect candidates.** The AI's value first (if it said *stated* and passed validation), then the rule extractor's value (if it passed validation).
2. **No candidates.** If the product already has a valid value, there is nothing to propose. If the attribute is optional and not critical or high importance, skip it. Otherwise store an **unknown** suggestion: no value, confidence 0, an explanation ("The product content does not state the capacity" or "The current value is invalid and the content does not state a correct one") and any AI rejection reasons. These tell the merchant exactly which values to fill in by hand.
3. **Pick the first candidate** (the AI's when there is one) as the proposed value.
4. **Cross-check the others.** If the rule extractor read the same value (units converted, 2% tolerance), confidence becomes the higher of the two and the explanation adds "Confirmed by rule-based extraction". If it read a different value, the suggestion becomes a *warning* saying what rules read instead, and confidence is capped at **0.5**.
5. **Compare with what the product already has.** Same value: nothing to propose, skip. Different value: still proposed, but as a *warning* that says "approving replaces it".
6. **Record `rule_confirmed`** when the rule extractor's value equals the final value. Auto-approval depends on it.

The AI's issues become findings with codes like `ai_contradiction` or `ai_unsupported_claim`. An issue whose quote cannot be found in the product keeps its message but loses the quote, and its confidence is capped at 0.5.

### 6.1 What a suggestion records

| Field | Example |
|---|---|
| attribute, data type | `capacity`, `volume` |
| current value | `null` (or the existing raw value) |
| proposed value | `{"value": 28, "unit": "l"}` |
| evidence, evidence source | "28 L capacity with a padded back panel.", `description` |
| confidence | 0.9 |
| method | `rule_extraction`, `llm:claude-opus-5-5`, later `+merchant_edit` if edited |
| explanation | "Stated in the product description. Confirmed by rule-based extraction." |
| validation status, errors | `valid`, `[]` |
| rule confirmed | true |
| status | `pending` |

## 7. Running a job

1. The merchant selects products on the **Catalog audit** page, or asks for "the N products with the lowest readiness". `POST /api/v1/merchant/catalog/enrichment-jobs` accepts up to `ENRICHMENT_MAX_PRODUCTS` (default 250) products that the audit has already imported. Only one job per store runs at a time. AI is used only when requested and a provider is configured.
2. The job is a row in `enrichment_jobs` on the same durable Postgres queue as audits: claimed with `FOR UPDATE SKIP LOCKED`, heartbeats, fenced writes, retries with backoff (see section 8.2 of the audit guide).
3. For each product the worker **re-reads the product live from Shopify** (so suggestions reflect the current data, not the last audit), runs the AI if enabled, builds suggestions, and stores that product's outcome in one transaction.
4. A Shopify error on one product is recorded against that product and the job carries on. A product that no longer exists is marked removed and its open suggestions are closed.
5. The job is resumable: products already in the job's results are skipped when a retried attempt picks it up. A "retry" button queues a new job for just the products that failed.

**When a product's results are stored:**

- its older `pending` suggestions become `superseded` (replaced by the newer analysis);
- attributes that already have an `approved` suggestion are left alone, so an approval waiting to be published is never silently replaced;
- its previous AI findings are replaced by the new ones;
- any suggestion that meets the schema's auto-approval rule is approved and logged (next section).

## 8. Auto-approval (opt-in)

Off for every schema until the merchant turns it on, per category, on the **Auto-approval** page. A suggestion is approved without review only if **all** of these hold:

- the attribute is in the rule's list (or the rule covers all attributes);
- there is a proposed value;
- it fills an empty attribute (it never replaces an existing value);
- validation status is *valid* (no warnings);
- the rule-based extractor read the same value (`rule_confirmed`);
- confidence is at least the threshold (default **0.9**, allowed 0.5 to 1.0).

In practice, with the default threshold, a value qualifies when the AI and the rules agree on it, or when rules alone found a measurement next to its label (0.9). Values found only by vocabulary rules (0.6 to 0.75) need a lower threshold.

Every auto-approval is logged in `catalog_changes` with the rule, confidence, method and evidence. The page previews what a rule *would* approve, and why the rest would not, even while it is off, and can apply it to pending suggestions on demand. Auto-approved values can be sent back to review, and **auto-approval is not publishing**: they still go through the preview and a confirmed publish.

## 9. Review

The **Review queue** lists suggestions with their evidence, confidence and validation result, filterable by status, product, attribute and validation status. Actions (one at a time or in bulk through `POST /api/v1/merchant/catalog/suggestions/review`):

| Action | Allowed from | Effect |
|---|---|---|
| Approve | pending, approved, failed | Approves the proposed value. Refused for unknown values (nothing to approve) and *invalid* ones. |
| Edit and approve | pending, approved, failed | The merchant's value is parsed and validated on the server; warnings are kept. Method gains `+merchant_edit`. This is also how unknown attributes get filled. |
| Reject | pending, approved, failed | Marks it rejected. |
| Back to review | approved | Returns it to pending (useful for auto-approved values). |

Each decision is a row in `catalog_changes`, with the value before and after.

### 9.1 Suggestion lifecycle

```mermaid
stateDiagram-v2
  [*] --> pending: enrichment job
  pending --> approved: approve / edit / auto-approval
  pending --> rejected: reject
  pending --> superseded: newer job, or product deleted
  approved --> pending: back to review
  approved --> published: write succeeded or value already set
  approved --> failed: Shopify refused the write
  approved --> superseded: product deleted
  failed --> approved: approve again (retry)
  failed --> rejected: reject
  published --> reverted: merchant reverts the write
```

## 10. Writing to Shopify

### 10.1 Where each value goes

Each attribute writes to one metafield: the schema's default (for example `custom.capacity_liters`, type `number_decimal`) unless the merchant changed it on the **Metafield mapping** page (namespace, key, type, or turned off). Values are converted to what the metafield type expects (`serialize`):

| Attribute type | Compatible metafield types | Stored as |
|---|---|---|
| text | `single_line_text_field`, `multi_line_text_field` | the text (single-line refuses line breaks) |
| list | `list.single_line_text_field`, `single_line_text_field` | a JSON list, or comma-joined text |
| number | `number_decimal`, `number_integer` | the number (integer refuses fractions) |
| boolean | `boolean` | `true` / `false` |
| weight, volume, dimension | the matching Shopify measurement type, or `number_decimal` | `{"value": 850, "unit": "g"}`, or the number converted to the attribute's unit (28 L → `28` for a liters field) |

### 10.2 The preview

`POST /api/v1/merchant/catalog/publish/preview` (up to `PUBLISH_PREVIEW_MAX`, default 250) shows, for each approved value, the metafield, its current value and the new one, and one of four actions:

| Action | Meaning |
|---|---|
| create | the product has no value there yet; a metafield definition will be created if the store has none |
| update | replaces the product's current value |
| unchanged | the product already has exactly this value |
| blocked | will not be written, with the reason: mapping turned off, no mapping for the attribute, value does not fit the type, the store already defines that `namespace.key` as a different type, the product already has that field with a different type, or the product no longer exists |

A second list shows the standard category metafield writes (section 10.4).

### 10.3 Publishing

`POST /api/v1/merchant/catalog/publish` with `confirm: true` queues a background **publish** job (`catalog_jobs`, kind `publish`) and returns straight away. Only one publish per store runs at a time, up to `PUBLISH_MAX_SUGGESTIONS` (default 5,000) values per job. The Review queue shows its progress.

The job works in batches of 25 (the most `metafieldsSet` accepts per call):

1. Lock the next approved suggestions with `FOR UPDATE SKIP LOCKED`, so a value is never written twice even with several workers.
2. Re-plan them against Shopify's current state (same checks as the preview).
3. Create any missing metafield definitions, so the values are typed and visible in Shopify admin as "Managed by Agentic Commerce Gateway catalog enrichment".
4. Write with `metafieldsSet`, passing each existing value's **`compareDigest`**: a fingerprint of the value the plan saw. If someone edited that field in Shopify admin in the meantime, Shopify rejects the write instead of overwriting their edit.
5. Record the outcome: `published` (or `unchanged`), or `failed` with Shopify's message. Each write is a `publish` row in `catalog_changes` with the value before and after.
6. Patch the product snapshot with the new value, write category metafields, and commit the batch together with the job's progress. A worker restart resumes from the next unpublished value.
7. Queue a single-product **refresh** for each product written, which re-scores it (section 8.3 of the audit guide).

### 10.4 Standard category metafields

Shopify's own search, filters and agent catalog read a product's **category metafields**: the standard attributes its taxonomy category defines (Color, Material, Activity), stored in the reserved `shopify` namespace as lists of references to taxonomy values. An attribute opts in with `taxonomy_attribute`; by default material maps to **Material** and intended use to **Activity**.

For each approved value, `taxonomy_writer.py`:

1. reads the product's category and its choice-list attributes, and finds the one with that name (an exact match, or a single attribute whose name ends with it, so "Material" also finds "Bag/Case material");
2. matches the value to that attribute's taxonomy values by whole words, ignoring case; values that match nothing are skipped and listed, never guessed;
3. finds the store's category metafield definition and the metaobject entries for the matched values, creating missing entries on publish (this needs the `write_metaobjects` scope);
4. adds the entries to the product's existing list, never removing what the merchant chose, and writes it with `compareDigest`.

Anything missing (no category, no matching attribute, no definition, no scope) makes that item a skip with the reason. The custom metafield write is never affected.

### 10.5 Reverting

Every `publish` change on the **Sync and jobs** page has a revert button (`POST /api/v1/merchant/catalog/changes/{id}/revert`). It restores the value recorded before the write, or deletes the metafield if the app created it. The change is marked reverted, a `revert` row is logged, the suggestion becomes `reverted`, and a refresh re-scores the product.

---

## 11. Worked example

Continuing the "Trailhead 28L Daypack" from the audit guide. Its description:

> A lightweight daypack for hiking and everyday carry. 28 L capacity with a padded back panel. Made from ripstop nylon. Weighs 850 g.

With an AI provider configured, the job produces:

| Attribute | Rules read | AI read | Suggestion | Confidence | Status |
|---|---|---|---|---|---|
| capacity | 28 L, "28 L capacity…" (label in sentence) | "28 L" | 28 l | 0.9, rule-confirmed | valid |
| weight | 850 g, "Weighs 850 g." | "850 g" | 850 g | 0.9, rule-confirmed | valid |
| material | ripstop nylon (vocabulary) | "ripstop nylon" | ripstop nylon | max(AI, 0.75), rule-confirmed | valid |
| intended use | hiking, everyday (vocabulary) | "hiking, everyday" | hiking, everyday | max(AI, 0.75), rule-confirmed | valid |
| laptop compartment, water resistance, max laptop size, carry-on, care | nothing | unknown | none stored (optional, not stated) | | |

Suppose instead the description said "30 L capacity" while the title still says "28L", and the AI answered "28 L" quoting the title. Both quotes pass validation. The AI's 28 L is proposed (AI candidates come first), but rules read 30 L, because a measurement next to its label (0.9) beats one found only in the title (0.7). The suggestion becomes a *warning* "rule-based extraction read 30 l instead" with confidence capped at 0.5, and it cannot auto-approve. The merchant decides.

If the description had said only "spacious", capacity would be stored as an **unknown** suggestion ("The product content does not state the capacity"), and the merchant would type the value in with edit-and-approve.

With auto-approval on for Backpacks at the default 0.9, capacity and weight are approved automatically (they fill empty attributes, are valid, rule-confirmed and at 0.9). Material and intended use auto-approve only if the AI's confidence lifted them to 0.9 or more. After the merchant confirms the publish, the four values land in `custom.capacity_liters` (`28`), `custom.weight` (`{"value":850,"unit":"g"}`), `custom.material` and `custom.intended_use`, plus `shopify.material` and `shopify.activity` if the category's taxonomy has matching values, and the refresh moves the product's readiness from 35 to 84.

---

## 12. Safety summary

| Risk | Defence |
|---|---|
| Invented specifications | Values need a quote that exists in the product and states the value; otherwise unknown. |
| Prompt injection in product text | Product text is sent as JSON data inside delimiters; the prompt forbids following it; instruction-like sentences are dropped from rules and rejected as evidence; a test covers an "ignore previous instructions" description. |
| AI output that does not fit | Structured outputs plus server-side schema validation; malformed output falls back to rules. |
| Overwriting merchant data | Approval required; preview shows replacements; `compareDigest` blocks writes over concurrent edits; type conflicts block rather than create duplicates. |
| Mistakes after publishing | Every write logged with the previous value and revertible in one click. |
| Cross-store access | Every query is scoped to the authenticated shop; suggestion ids from another store are ignored. |
| Unwanted automation | Auto-approval off by default, narrow conditions, logged, and still needs a confirmed publish. |

## 13. Known gaps

- Values set by a meta mapping rule count in the audit, but enrichment's own "current value" check reads only metafields and options, so it may propose a value a mapping rule already supplies.
- The AI sees one product at a time; it does not compare across the catalog.
- Taxonomy metaobject shapes follow Shopify's documentation and are exercised against a fake API in tests, not a live store.
