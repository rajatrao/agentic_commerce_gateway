import { useEffect, useState } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import {
  useFetcher,
  useLoaderData,
  useNavigate,
  useSearchParams,
} from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, errorMessage } from "../lib/backend.server";
import {
  findingLabel,
  productId,
  ReadinessBadge,
  SEVERITIES,
  SeverityCounts,
} from "../components/Catalog";

const PAGE_SIZE = 50;
const FILTERS = ["q", "schema", "severity", "code", "max_readiness"] as const;

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const url = new URL(request.url);
  const page = Math.max(1, Number(url.searchParams.get("page") ?? 1) || 1);
  const filters: Record<string, string> = {};
  for (const key of FILTERS) {
    const value = url.searchParams.get(key);
    if (value) filters[key] = value;
  }
  // Older links from the Discovery Score widgets used max_score/issue.
  if (!filters.max_readiness && url.searchParams.get("max_score"))
    filters.max_readiness = url.searchParams.get("max_score")!;
  if (!filters.code && url.searchParams.get("issue"))
    filters.code = url.searchParams.get("issue")!;

  const [products, overview, schemas] = await Promise.all([
    backend.catalogProducts(session.shop, {
      ...filters,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    backend.catalogOverview(session.shop),
    backend.schemas(session.shop),
  ]);
  return {
    products,
    page,
    filters,
    hasAudit: overview.has_audit,
    aiEnabled: overview.ai_enabled,
    threshold: overview.ready_threshold,
    codes: overview.top_findings.map((f) => f.code),
    schemas: schemas.map((s) => ({ key: s.key, name: s.definition.name })),
  };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const useAi = form.get("use_ai") === "true";
  const gids = form.getAll("gid").map(String);
  try {
    const job = await backend.startEnrichment(
      session.shop,
      gids.length
        ? { product_gids: gids, use_ai: useAi }
        : { lowest_readiness: 25, use_ai: useAi },
    );
    return {
      ok: true,
      message: `Generating suggestions for ${job.total} products`,
      error: null,
    };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

export default function Products() {
  const {
    products,
    page,
    filters,
    hasAudit,
    aiEnabled,
    threshold,
    codes,
    schemas,
  } = useLoaderData<typeof loader>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [useAi, setUseAi] = useState(aiEnabled);
  const [search, setSearch] = useState(filters.q ?? "");
  const busy = fetcher.state !== "idle";

  // Clear the selection once per successful submit (state adjusted during render).
  const [handled, setHandled] = useState(fetcher.data);
  if (fetcher.data !== handled) {
    setHandled(fetcher.data);
    if (fetcher.data?.ok) setSelected(new Set());
  }

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const go = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    next.delete("max_score");
    next.delete("issue");
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    navigate(`/app/products?${next}`);
  };

  const toggle = (gid: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(gid);
    else next.delete(gid);
    setSelected(next);
  };

  const enrich = (gids: string[]) => {
    const form = new FormData();
    form.set("use_ai", String(useAi));
    gids.forEach((gid) => form.append("gid", gid));
    fetcher.submit(form, { method: "POST" });
  };

  const totalPages = Math.max(1, Math.ceil(products.total / PAGE_SIZE));
  const codeOptions =
    filters.code && !codes.includes(filters.code)
      ? [filters.code, ...codes]
      : codes;

  return (
    <s-page heading="Catalog audit">
      <s-button
        slot="primary-action"
        variant="primary"
        disabled={!hasAudit}
        onClick={() => enrich([...selected])}
        {...(busy ? { loading: true } : {})}
      >
        {selected.size
          ? `Enrich ${selected.size} selected`
          : "Enrich 25 lowest-scoring"}
      </s-button>

      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Could not start enrichment">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      {!hasAudit ? (
        <s-section>
          <s-paragraph>
            No completed catalog audit yet. Run one from the{" "}
            <s-link href="/app">dashboard</s-link>.
          </s-paragraph>
        </s-section>
      ) : (
        <s-section>
          <s-stack direction="block" gap="base">
            <s-grid
              gridTemplateColumns="repeat(auto-fill, minmax(200px, 1fr))"
              gap="base"
            >
              <s-search-field
                label="Search products"
                value={search}
                onInput={(e) => setSearch(e.currentTarget.value)}
                onChange={(e) =>
                  go({ q: e.currentTarget.value || null, page: null })
                }
              />
              <s-select
                label="Category schema"
                value={filters.schema ?? ""}
                onChange={(e) =>
                  go({ schema: e.currentTarget.value || null, page: null })
                }
              >
                <s-option value="">All categories</s-option>
                {schemas.map((s) => (
                  <s-option key={s.key} value={s.key}>
                    {s.name}
                  </s-option>
                ))}
              </s-select>
              <s-select
                label="Severity"
                value={filters.severity ?? ""}
                onChange={(e) =>
                  go({ severity: e.currentTarget.value || null, page: null })
                }
              >
                <s-option value="">Any severity</s-option>
                <s-option value="critical,high">Critical or high</s-option>
                {SEVERITIES.map((s) => (
                  <s-option key={s} value={s}>
                    {s}
                  </s-option>
                ))}
              </s-select>
              <s-select
                label="Finding"
                value={filters.code ?? ""}
                onChange={(e) =>
                  go({ code: e.currentTarget.value || null, page: null })
                }
              >
                <s-option value="">Any finding</s-option>
                {codeOptions.map((code) => (
                  <s-option key={code} value={code}>
                    {findingLabel(code)}
                  </s-option>
                ))}
              </s-select>
              <s-select
                label="Readiness"
                value={filters.max_readiness ?? ""}
                onChange={(e) =>
                  go({
                    max_readiness: e.currentTarget.value || null,
                    page: null,
                  })
                }
              >
                <s-option value="">Any score</s-option>
                <s-option value={String(threshold - 1)}>
                  {`Not agent-ready (below ${threshold})`}
                </s-option>
                <s-option value="59">Below 60</s-option>
                <s-option value="39">Below 40</s-option>
              </s-select>
            </s-grid>

            <s-stack direction="inline" gap="base" alignItems="center">
              <s-text color="subdued">{`${products.total} products, lowest readiness first`}</s-text>
              <s-checkbox
                label="Use Claude for suggestions"
                checked={useAi}
                disabled={!aiEnabled}
                details={
                  aiEnabled
                    ? undefined
                    : "Add an Anthropic API key to the engine to enable"
                }
                onChange={(e) => setUseAi(e.currentTarget.checked)}
              />
            </s-stack>

            {products.items.length === 0 ? (
              <s-paragraph>No products match.</s-paragraph>
            ) : (
              <s-table variant="auto">
                <s-table-header-row>
                  <s-table-header>Select</s-table-header>
                  <s-table-header listSlot="primary">Product</s-table-header>
                  <s-table-header listSlot="secondary" format="numeric">
                    Readiness
                  </s-table-header>
                  <s-table-header listSlot="labeled">Findings</s-table-header>
                  <s-table-header>Category schema</s-table-header>
                </s-table-header-row>
                <s-table-body>
                  {products.items.map((p) => (
                    <s-table-row key={p.product_gid}>
                      <s-table-cell>
                        <s-checkbox
                          accessibilityLabel={`Select ${p.title}`}
                          checked={selected.has(p.product_gid)}
                          onChange={(e) =>
                            toggle(p.product_gid, e.currentTarget.checked)
                          }
                        />
                      </s-table-cell>
                      <s-table-cell>
                        <s-stack direction="block" gap="small-300">
                          <s-link
                            href={`/app/products/${productId(p.product_gid)}`}
                          >
                            {p.title || "Untitled product"}
                          </s-link>
                          <s-text color="subdued">
                            {[p.vendor, p.product_type]
                              .filter(Boolean)
                              .join(" · ")}
                          </s-text>
                        </s-stack>
                      </s-table-cell>
                      <s-table-cell>
                        <ReadinessBadge
                          score={p.readiness_score}
                          threshold={threshold}
                        />
                      </s-table-cell>
                      <s-table-cell>
                        <SeverityCounts counts={p.findings} />
                      </s-table-cell>
                      <s-table-cell>
                        {schemas.find((s) => s.key === p.schema_key)?.name ??
                          p.schema_key ??
                          "-"}
                      </s-table-cell>
                    </s-table-row>
                  ))}
                </s-table-body>
              </s-table>
            )}

            <s-stack direction="inline" gap="base" alignItems="center">
              <s-button
                disabled={page <= 1}
                onClick={() => go({ page: String(page - 1) })}
              >
                Previous
              </s-button>
              <s-text>{`Page ${page} of ${totalPages}`}</s-text>
              <s-button
                disabled={page >= totalPages}
                onClick={() => go({ page: String(page + 1) })}
              >
                Next
              </s-button>
            </s-stack>
          </s-stack>
        </s-section>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
