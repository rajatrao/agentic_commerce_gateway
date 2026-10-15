import { mintServiceToken, type ServiceScope } from "./jwt.server";
import type {
  AgentClient,
  AgentClientCreated,
  AgentProtocol,
  AuditRun,
  PaymentSettings,
  TransactionPage,
  AuditSummary,
  InstallationCheck,
  MerchantSyncResult,
  ProductAuditPage,
} from "./types";

/**
 * Typed client for backend-engine. Runs only on the server: the browser never talks to
 * FastAPI directly. Every call carries a freshly minted service JWT for the merchant.
 */

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

  startAudit(shop: string) {
    return backendRequest<AuditRun>(shop, "/api/v1/merchant/audits", {
      method: "POST",
    });
  },

  auditSummary(shop: string) {
    return backendRequest<AuditSummary>(shop, "/api/v1/merchant/audits/latest");
  },

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

  agentClients(shop: string) {
    return backendRequest<AgentClient[]>(
      shop,
      "/api/v1/merchant/agent-clients",
    );
  },

  createAgentClient(shop: string, name: string, protocol: AgentProtocol) {
    return backendRequest<AgentClientCreated>(
      shop,
      "/api/v1/merchant/agent-clients",
      { method: "POST", body: { name, protocol } },
    );
  },

  revokeAgentClient(shop: string, id: string) {
    return backendRequest<void>(
      shop,
      `/api/v1/merchant/agent-clients/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },

  transactions(shop: string, limit = 50, offset = 0) {
    return backendRequest<TransactionPage>(
      shop,
      `/api/v1/merchant/transactions?limit=${limit}&offset=${offset}`,
    );
  },

  paymentSettings(shop: string) {
    return backendRequest<PaymentSettings>(shop, "/api/v1/merchant/payments");
  },

  savePaymentSettings(shop: string, stripeAccountId: string | null) {
    return backendRequest<PaymentSettings>(shop, "/api/v1/merchant/payments", {
      method: "PUT",
      body: { stripe_account_id: stripeAccountId },
    });
  },

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
};
