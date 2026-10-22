import { mintServiceToken, type ServiceScope } from "./jwt.server";
import type {
  AgentClient,
  AutoApprovalReport,
  AutoApprovalRule,
  CatalogChange,
  CatalogJob,
  CatalogFinding,
  CatalogOverview,
  CatalogProduct,
  CatalogSettings,
  CategorySchema,
  EnrichmentJob,
  MappingPreview,
  MappingRule,
  MappingRuleInput,
  MetafieldMappingEntry,
  Page,
  ProductDetail,
  RunPoint,
  PublishPreview,
  ReviewAction,
  ReviewResult,
  SchemaEntry,
  Suggestion,
  AgentCheckoutPage,
  AgentClientCreated,
  AgentProtocol,
  AuditRun,
  AuditSummary,
  InstallationCheck,
  MerchantSyncResult,
  ProductAuditPage,
} from "./types";

/**
 * Typed client for backend-engine. Runs only on the server: the browser never talks to
 * FastAPI directly. Every call carries a freshly minted service JWT for the merchant.
 */

/** A non-2xx response from backend-engine, with its status and `detail`. */
export class BackendError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`backend-engine responded ${status}: ${detail}`);
    this.name = "BackendError";
  }
}

const DEFAULT_TIMEOUT_MS = 30_000;
const CATALOG = "/api/v1/merchant/catalog";

/** Builds a query string, skipping empty values. */
export function toQuery(params: Record<string, string | number | boolean>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== undefined && value !== null)
      query.set(key, String(value));
  }
  return query.toString();
}

function baseUrl(): string {
  const url = process.env.BACKEND_INTERNAL_URL;
  if (!url) {
    throw new Error("BACKEND_INTERNAL_URL is not configured");
  }
  return url.replace(/\/$/, "");
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  scope?: ServiceScope[];
  timeoutMs?: number;
}

/**
 * Calls a backend-engine route as `shop` with a fresh service JWT and returns
 * the parsed JSON. Throws `BackendError` on a non-2xx response.
 */
export async function backendRequest<T>(
  shop: string,
  path: string,
  {
    method = "GET",
    body,
    scope,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  }: RequestOptions = {},
): Promise<T> {
  const token = await mintServiceToken(shop, scope);
  const response = await fetch(`${baseUrl()}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const json = (await response.json()) as { detail?: unknown };
      if (json.detail) {
        detail =
          typeof json.detail === "string"
            ? json.detail
            : JSON.stringify(json.detail);
      }
    } catch {
      // non-JSON error body
    }
    throw new BackendError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** One method per backend-engine route used by the admin UI. */
export const backend = {
  /** Called from the Shopify `afterAuth` hook: registers the shop and hands over its token. */
  syncMerchant(shop: string, accessToken: string, scopes: string | undefined) {
    return backendRequest<MerchantSyncResult>(
      shop,
      "/internal/merchants/sync",
      {
        method: "POST",
        body: { shop, access_token: accessToken, scopes: scopes ?? "" },
        scope: ["internal:sync"],
      },
    );
  },

  /** The most recent installation check, or null if none has run. */
  async latestInstallationCheck(
    shop: string,
  ): Promise<InstallationCheck | null> {
    try {
      return await backendRequest<InstallationCheck>(
        shop,
        "/api/v1/merchant/installation-checks/latest",
      );
    } catch (error) {
      if (error instanceof BackendError && error.status === 404) return null;
      throw error;
    }
  },

  /** Queues a catalog audit. */
  startAudit(shop: string) {
    return backendRequest<AuditRun>(shop, "/api/v1/merchant/audits", {
      method: "POST",
    });
  },

  /** The latest audit run and the latest completed audit's summary. */
  auditSummary(shop: string) {
    return backendRequest<AuditSummary>(shop, "/api/v1/merchant/audits/latest");
  },

  /**
   * A page of the latest audit's product scores, or null before the first audit
   * completes.
   */
  async auditProducts(
    shop: string,
    params: {
      maxScore?: number;
      issue?: string;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<ProductAuditPage | null> {
    const query = new URLSearchParams();
    if (params.maxScore !== undefined)
      query.set("max_score", String(params.maxScore));
    if (params.issue) query.set("issue", params.issue);
    query.set("limit", String(params.limit ?? 50));
    query.set("offset", String(params.offset ?? 0));
    try {
      return await backendRequest<ProductAuditPage>(
        shop,
        `/api/v1/merchant/audits/latest/products?${query}`,
      );
    } catch (error) {
      if (error instanceof BackendError && error.status === 404) return null;
      throw error;
    }
  },

  /** The store's agent API keys. */
  agentClients(shop: string) {
    return backendRequest<AgentClient[]>(
      shop,
      "/api/v1/merchant/agent-clients",
    );
  },

  /**
   * Issues an agent API key. The response holds the only copy of the plaintext
   * key.
   */
  createAgentClient(shop: string, name: string, protocol: AgentProtocol) {
    return backendRequest<AgentClientCreated>(
      shop,
      "/api/v1/merchant/agent-clients",
      { method: "POST", body: { name, protocol } },
    );
  },

  /** Revokes an agent API key. */
  revokeAgentClient(shop: string, id: string) {
    return backendRequest<void>(
      shop,
      `/api/v1/merchant/agent-clients/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },

  /** A page of carts that agents built for the store. */
  agentCheckouts(shop: string, limit = 50, offset = 0) {
    return backendRequest<AgentCheckoutPage>(
      shop,
      `/api/v1/merchant/agent-checkouts?limit=${limit}&offset=${offset}`,
    );
  },

  /** Runs a storefront installation check (can take up to a minute). */
  runInstallationCheck(shop: string, storeUrl?: string) {
    return backendRequest<InstallationCheck>(
      shop,
      "/api/v1/merchant/installation-checks",
      {
        method: "POST",
        body: storeUrl ? { store_url: storeUrl } : {},
        timeoutMs: 60_000,
      },
    );
  },

  // ---------------------------------------------------------------- catalog intelligence

  /** Dashboard numbers for catalog readiness. */
  catalogOverview(shop: string) {
    return backendRequest<CatalogOverview>(shop, `${CATALOG}/overview`);
  },

  /** Recent audit runs. */
  auditRuns(shop: string, limit = 20) {
    return backendRequest<RunPoint[]>(shop, `${CATALOG}/runs?limit=${limit}`);
  },

  /** A filtered page of audited products. */
  catalogProducts(shop: string, params: Record<string, string | number> = {}) {
    return backendRequest<Page<CatalogProduct>>(
      shop,
      `${CATALOG}/products?${toQuery(params)}`,
    );
  },

  /** One product's detail, or null if it has not been audited. */
  async catalogProduct(
    shop: string,
    productId: string,
  ): Promise<ProductDetail | null> {
    try {
      return await backendRequest<ProductDetail>(
        shop,
        `${CATALOG}/products/${encodeURIComponent(productId)}`,
      );
    } catch (error) {
      if (error instanceof BackendError && error.status === 404) return null;
      throw error;
    }
  },

  /** A filtered page of current catalog findings. */
  catalogFindings(shop: string, params: Record<string, string | number> = {}) {
    return backendRequest<Page<CatalogFinding>>(
      shop,
      `${CATALOG}/findings?${toQuery(params)}`,
    );
  },

  /** Queues an enrichment job for chosen products or the least-ready ones. */
  startEnrichment(
    shop: string,
    body: {
      product_gids?: string[];
      lowest_readiness?: number;
      use_ai: boolean;
    },
  ) {
    return backendRequest<EnrichmentJob>(shop, `${CATALOG}/enrichment-jobs`, {
      method: "POST",
      body,
    });
  },

  /** Recent enrichment jobs. */
  enrichmentJobs(shop: string) {
    return backendRequest<EnrichmentJob[]>(shop, `${CATALOG}/enrichment-jobs`);
  },

  /** One enrichment job with its per-product results. */
  enrichmentJob(shop: string, id: string) {
    return backendRequest<EnrichmentJob>(
      shop,
      `${CATALOG}/enrichment-jobs/${encodeURIComponent(id)}`,
    );
  },

  /** Queues a new enrichment job for the products that failed in this one. */
  retryEnrichmentJob(shop: string, id: string) {
    return backendRequest<EnrichmentJob>(
      shop,
      `${CATALOG}/enrichment-jobs/${encodeURIComponent(id)}/retry`,
      { method: "POST" },
    );
  },

  /** A filtered page of the review queue. */
  suggestions(shop: string, params: Record<string, string | number> = {}) {
    return backendRequest<Page<Suggestion>>(
      shop,
      `${CATALOG}/suggestions?${toQuery(params)}`,
    );
  },

  /** Approves, edits, rejects or reopens suggestions in bulk. */
  reviewSuggestions(shop: string, actions: ReviewAction[]) {
    return backendRequest<ReviewResult>(shop, `${CATALOG}/suggestions/review`, {
      method: "POST",
      body: { actions },
    });
  },

  /**
   * What publishing the approved suggestions would write to Shopify. Writes
   * nothing.
   */
  previewPublish(shop: string, suggestionIds?: string[]) {
    return backendRequest<PublishPreview>(shop, `${CATALOG}/publish/preview`, {
      method: "POST",
      body: { suggestion_ids: suggestionIds ?? null },
      timeoutMs: 120_000,
    });
  },

  /** Queues a background publish job; poll `publishJob` for progress. */
  publish(shop: string, suggestionIds?: string[]) {
    return backendRequest<CatalogJob>(shop, `${CATALOG}/publish`, {
      method: "POST",
      body: { suggestion_ids: suggestionIds ?? null, confirm: true },
    });
  },

  /** One publish job's progress and results. */
  publishJob(shop: string, id: string) {
    return backendRequest<CatalogJob>(
      shop,
      `${CATALOG}/publish-jobs/${encodeURIComponent(id)}`,
    );
  },

  /** Recent publish jobs. */
  publishJobs(shop: string, limit = 10) {
    return backendRequest<CatalogJob[]>(
      shop,
      `${CATALOG}/publish-jobs?limit=${limit}`,
    );
  },

  /**
   * Queues a run of the schemas' shopper questions against the store's search.
   */
  startDiscoveryRun(shop: string) {
    return backendRequest<CatalogJob>(shop, `${CATALOG}/discovery-runs`, {
      method: "POST",
    });
  },

  /** Recent discovery test runs. */
  discoveryRuns(shop: string, limit = 10) {
    return backendRequest<CatalogJob[]>(
      shop,
      `${CATALOG}/discovery-runs?limit=${limit}`,
    );
  },

  /** Auto-approval rules per schema and what they recently approved. */
  autoApprovalReport(shop: string) {
    return backendRequest<AutoApprovalReport>(shop, `${CATALOG}/auto-approval`);
  },

  /** Saves a schema's auto-approval rule. */
  saveAutoApproval(shop: string, schemaKey: string, rule: AutoApprovalRule) {
    return backendRequest<SchemaEntry>(
      shop,
      `${CATALOG}/schemas/${encodeURIComponent(schemaKey)}/auto-approval`,
      { method: "PUT", body: rule },
    );
  },

  /** Applies a schema's enabled rule to its pending suggestions now. */
  applyAutoApproval(shop: string, schemaKey: string) {
    return backendRequest<{ approved: number }>(
      shop,
      `${CATALOG}/auto-approval/${encodeURIComponent(schemaKey)}/apply`,
      { method: "POST" },
    );
  },

  /** A filtered page of the catalog change log. */
  catalogChanges(
    shop: string,
    params: Record<string, string | number | boolean> = {},
  ) {
    return backendRequest<Page<CatalogChange>>(
      shop,
      `${CATALOG}/changes?${toQuery(params)}`,
    );
  },

  /** Restores a published value in Shopify to what it was before. */
  revertChange(shop: string, id: number) {
    return backendRequest<CatalogChange>(
      shop,
      `${CATALOG}/changes/${id}/revert`,
      { method: "POST", timeoutMs: 60_000 },
    );
  },

  /** Category schemas in effect for the store. */
  schemas(shop: string) {
    return backendRequest<SchemaEntry[]>(shop, `${CATALOG}/schemas`);
  },

  /** Saves the store's custom version of a category schema. */
  saveSchema(shop: string, schema: CategorySchema) {
    return backendRequest<SchemaEntry>(
      shop,
      `${CATALOG}/schemas/${encodeURIComponent(schema.key)}`,
      { method: "PUT", body: schema },
    );
  },

  /**
   * Drops the store's version of a schema: a built-in one reverts to its default,
   * an added one is removed.
   */
  resetSchema(shop: string, key: string) {
    return backendRequest<void>(
      shop,
      `${CATALOG}/schemas/${encodeURIComponent(key)}`,
      { method: "DELETE" },
    );
  },

  /**
   * Where each attribute is written in Shopify, optionally checked against the
   * store's definitions.
   */
  mappings(shop: string, checkShopify = false) {
    return backendRequest<MetafieldMappingEntry[]>(
      shop,
      `${CATALOG}/mappings?check_shopify=${checkShopify}`,
      { timeoutMs: 60_000 },
    );
  },

  /** Changes the metafield an attribute is written to. */
  saveMapping(
    shop: string,
    attributeKey: string,
    body: { namespace: string; key: string; type: string; enabled: boolean },
  ) {
    return backendRequest<MetafieldMappingEntry>(
      shop,
      `${CATALOG}/mappings/${encodeURIComponent(attributeKey)}`,
      { method: "PUT", body },
    );
  },

  /** The store's meta-mapping rules. */
  mappingRules(shop: string) {
    return backendRequest<MappingRule[]>(
      shop,
      "/api/v1/merchant/mapping-rules",
    );
  },

  /** Creates a meta-mapping rule. */
  createMappingRule(shop: string, body: MappingRuleInput) {
    return backendRequest<MappingRule>(shop, "/api/v1/merchant/mapping-rules", {
      method: "POST",
      body,
    });
  },

  /** Reads live from Shopify, so it gets a longer timeout. Saves nothing. */
  previewMappingRule(shop: string, body: MappingRuleInput) {
    return backendRequest<MappingPreview>(
      shop,
      "/api/v1/merchant/mapping-rules/preview",
      { method: "POST", body, timeoutMs: 60_000 },
    );
  },

  /** Turns a meta-mapping rule on or off. */
  setMappingRuleActive(shop: string, id: string, active: boolean) {
    return backendRequest<MappingRule>(
      shop,
      `/api/v1/merchant/mapping-rules/${encodeURIComponent(id)}`,
      { method: "PATCH", body: { active } },
    );
  },

  /** Deletes a meta-mapping rule. */
  deleteMappingRule(shop: string, id: string) {
    return backendRequest<void>(
      shop,
      `/api/v1/merchant/mapping-rules/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },

  /** The store's catalog settings. */
  catalogSettings(shop: string) {
    return backendRequest<CatalogSettings>(shop, `${CATALOG}/settings`);
  },

  /** Saves the readiness weights and ready threshold. */
  saveCatalogSettings(
    shop: string,
    body: {
      readiness_weights: Record<string, number>;
      ready_threshold: number;
    },
  ) {
    return backendRequest<CatalogSettings>(shop, `${CATALOG}/settings`, {
      method: "PUT",
      body,
    });
  },
};

/** A message for the merchant: the engine's own detail, or a generic outage message. */
export function errorMessage(error: unknown) {
  return error instanceof BackendError
    ? error.detail
    : "The gateway engine is unreachable.";
}
