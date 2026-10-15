import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { useLoaderData, useNavigate, useSearchParams } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend } from "../lib/backend.server";
import { ProductIssueTable } from "../components/DiscoveryScore";

const PAGE_SIZE = 50;

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const url = new URL(request.url);
  const maxScore = Number(url.searchParams.get("max_score") ?? 100);
  const issue = url.searchParams.get("issue") ?? undefined;
  const page = Math.max(1, Number(url.searchParams.get("page") ?? 1));

  const [products, summary] = await Promise.all([
    backend.auditProducts(session.shop, {
      maxScore: Number.isFinite(maxScore) ? maxScore : 100,
      issue,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    backend.auditSummary(session.shop),
  ]);
  return { products, issues: summary.top_issues, page, issue: issue ?? "" };
};

export default function Products() {
  const { products, issues, page, issue } = useLoaderData<typeof loader>();
  const [params] = useSearchParams();
  const navigate = useNavigate();

  const go = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    navigate(`/app/products?${next}`);
  };

  const totalPages = products
    ? Math.max(1, Math.ceil(products.total / PAGE_SIZE))
    : 1;

  return (
    <s-page heading="Product audit">
      <s-section>
        {!products ? (
          <s-paragraph>
            No completed catalog audit yet. Run one from the{" "}
            <s-link href="/app">dashboard</s-link>.
          </s-paragraph>
        ) : (
          <s-stack direction="block" gap="base">
            <s-select
              label="Filter by gap"
              value={issue}
              onChange={(event) =>
                go({ issue: event.currentTarget.value || null, page: null })
              }
            >
              <s-option value="">All products</s-option>
              {issues.map((i) => (
                <s-option key={i.code} value={i.code}>
                  {`${i.message} (${i.count})`}
                </s-option>
              ))}
            </s-select>
            <s-text color="subdued">{`${products.total} products, lowest score first`}</s-text>
            <ProductIssueTable products={products.items} />
            <s-stack direction="inline" gap="base">
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
        )}
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
