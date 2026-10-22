import type { AuditSummary, ProductAudit } from "../lib/types";

const DIMENSION_LABELS: Record<string, string> = {
  identity: "Identity",
  description: "Description",
  media: "Media",
  identifiers: "Identifiers (SKU/GTIN)",
  offers: "Offers",
  taxonomy: "Taxonomy",
  attributes: "Attributes",
  reviews: "Reviews",
};

const GRADES = [
  { key: "excellent", label: "Excellent (85+)", tone: "success" },
  { key: "good", label: "Good (70-84)", tone: "info" },
  { key: "needs_work", label: "Needs work (50-69)", tone: "warning" },
  { key: "poor", label: "Poor (<50)", tone: "critical" },
] as const;

/** Polaris badge tone for an AI Discovery Score grade. */
export function scoreTone(
  score: number,
): "success" | "info" | "warning" | "critical" {
  if (score >= 85) return "success";
  if (score >= 70) return "info";
  if (score >= 50) return "warning";
  return "critical";
}

/**
 * Card with the AI Discovery Score, grade counts and per-dimension averages.
 */
export function DiscoveryScore({ summary }: { summary: AuditSummary }) {
  const score = summary.ai_discovery_score
    ? Math.round(Number(summary.ai_discovery_score))
    : null;
  const run = summary.completed_run;
  if (score === null || !run) {
    return (
      <s-paragraph>
        No catalog audit yet. Run one to score every active product on the
        structured data AI agents rely on.
      </s-paragraph>
    );
  }

  return (
    <s-stack direction="block" gap="base">
      <s-stack direction="inline" gap="base" alignItems="center">
        <s-heading>{`${score} / 100`}</s-heading>
        <s-badge tone={scoreTone(score)} size="large">
          AI Discovery Score
        </s-badge>
      </s-stack>
      <s-text color="subdued">
        {run.products_scanned} products audited{" "}
        {run.finished_at ? new Date(run.finished_at).toLocaleString() : ""}
      </s-text>

      <s-stack direction="inline" gap="small-200">
        {GRADES.map(({ key, label, tone }) => (
          <s-badge
            key={key}
            tone={tone}
          >{`${label}: ${summary.grade_counts[key] ?? 0}`}</s-badge>
        ))}
      </s-stack>

      <s-table variant="auto">
        <s-table-header-row>
          <s-table-header listSlot="primary">Dimension</s-table-header>
          <s-table-header listSlot="secondary" format="numeric">
            Average
          </s-table-header>
        </s-table-header-row>
        <s-table-body>
          {Object.entries(DIMENSION_LABELS).map(([key, label]) => {
            const dim = summary.dimension_averages[key];
            return (
              <s-table-row key={key}>
                <s-table-cell>{label}</s-table-cell>
                <s-table-cell>
                  {dim ? `${dim.points} / ${dim.max}` : "-"}
                </s-table-cell>
              </s-table-row>
            );
          })}
        </s-table-body>
      </s-table>
    </s-stack>
  );
}

/**
 * List of the most common audit issues, each linking to the affected products.
 */
export function TopIssues({ summary }: { summary: AuditSummary }) {
  if (summary.top_issues.length === 0) return null;
  return (
    <s-unordered-list>
      {summary.top_issues.map((issue) => (
        <s-list-item key={issue.code}>
          <s-link
            href={`/app/products?issue=${encodeURIComponent(issue.code)}`}
          >
            {issue.message}
          </s-link>{" "}
          <s-text color="subdued">({issue.count} products)</s-text>
        </s-list-item>
      ))}
    </s-unordered-list>
  );
}

/** Table of products with their score and the first few things to fix. */
export function ProductIssueTable({ products }: { products: ProductAudit[] }) {
  if (products.length === 0) {
    return <s-paragraph>No products match.</s-paragraph>;
  }
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Product</s-table-header>
        <s-table-header listSlot="secondary" format="numeric">
          Score
        </s-table-header>
        <s-table-header listSlot="labeled">What to fix</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {products.map((product) => (
          <s-table-row key={product.product_gid}>
            <s-table-cell>
              <s-link href={product.admin_url} target="_blank">
                {product.title || "Untitled product"}
              </s-link>
            </s-table-cell>
            <s-table-cell>
              <s-badge tone={scoreTone(product.score)}>
                {String(product.score)}
              </s-badge>
            </s-table-cell>
            <s-table-cell>
              {product.issues.slice(0, 3).join(" ")}
              {product.issues.length > 3
                ? ` (+${product.issues.length - 3} more)`
                : ""}
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}
