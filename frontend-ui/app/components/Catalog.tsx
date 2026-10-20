import type {
  CatalogChange,
  CatalogFinding,
  FindingSeverity,
  Suggestion,
} from "../lib/types";

export const SEVERITIES: FindingSeverity[] = [
  "critical",
  "high",
  "medium",
  "low",
];

const SEVERITY_TONES: Record<
  FindingSeverity,
  "critical" | "warning" | "caution" | "neutral"
> = {
  critical: "critical",
  high: "warning",
  medium: "caution",
  low: "neutral",
};

export function severityTone(severity: FindingSeverity) {
  return SEVERITY_TONES[severity];
}

const FINDING_LABELS: Record<string, string> = {
  attribute_missing: "Required attribute missing",
  attribute_buried: "Attribute only in the description",
  attribute_invalid: "Attribute value is invalid",
  attribute_contradiction: "Description contradicts an attribute",
  claim_unsupported: "Claim without supporting detail",
  description_vague: "Description is vague",
  variant_option_missing: "Variant option missing",
  ai_contradiction: "Contradiction found by AI",
  ai_unsupported_claim: "Unsupported claim found by AI",
  ai_ambiguity: "Ambiguity found by AI",
  ai_missing_detail: "Missing detail found by AI",
};

/** Readiness score components, in display order. */
export const COMPONENT_LABELS: Record<string, string> = {
  completeness: "Required and important attributes filled in",
  validity: "Attribute values are valid",
  consistency: "Description agrees with attributes",
  evidence: "Claims are backed by specifics",
  variants: "Variant options are structured",
  foundation: "Core product data (AI Discovery Score)",
};

export function findingLabel(code: string) {
  if (FINDING_LABELS[code]) return FINDING_LABELS[code];
  const text = code.replace(/^ai_/, "").replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "gid://shopify/Product/123" -> "123", used in app URLs. */
export function productId(gid: string) {
  return gid.split("/").pop() ?? gid;
}

export function formatDate(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : "";
}

export function readinessTone(
  score: number | null,
  threshold = 80,
): "success" | "info" | "warning" | "critical" | "neutral" {
  if (score === null) return "neutral";
  if (score >= threshold) return "success";
  if (score >= 60) return "info";
  if (score >= 40) return "warning";
  return "critical";
}

export function ReadinessBadge({
  score,
  threshold,
}: {
  score: number | null;
  threshold?: number;
}) {
  return (
    <s-badge tone={readinessTone(score, threshold)}>
      {score === null ? "Not scored" : String(score)}
    </s-badge>
  );
}

export function SeverityCounts({
  counts,
}: {
  counts: Partial<Record<FindingSeverity, number>>;
}) {
  const present = SEVERITIES.filter((s) => (counts[s] ?? 0) > 0);
  if (present.length === 0) return <s-text color="subdued">None</s-text>;
  return (
    <s-stack direction="inline" gap="small-300">
      {present.map((s) => (
        <s-badge key={s} tone={severityTone(s)}>{`${counts[s]} ${s}`}</s-badge>
      ))}
    </s-stack>
  );
}

/** Renders a canonical attribute value ({value, unit} measurements, lists, booleans). */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (Array.isArray(value)) return value.map(displayValue).join(", ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") {
    const v = value as Record<string, unknown>;
    if ("value" in v && "unit" in v) return `${v.value} ${v.unit}`;
    if ("value" in v) return displayValue(v.value);
    return JSON.stringify(value);
  }
  return String(value);
}

/** The text a merchant edits for a suggestion: lists as comma-separated, measurements as "28 L". */
export function editableValue(s: Suggestion) {
  const value = s.approved_value ?? s.proposed_value;
  if (value === null || value === undefined) return "";
  return displayValue(value);
}

const VALIDATION_TONES = {
  valid: "success",
  warning: "warning",
  invalid: "critical",
  unknown: "neutral",
} as const;

export function ValidationBadge({ suggestion }: { suggestion: Suggestion }) {
  const label =
    suggestion.validation_status === "unknown"
      ? "Not stated"
      : suggestion.validation_status;
  return (
    <s-badge tone={VALIDATION_TONES[suggestion.validation_status]}>
      {label}
    </s-badge>
  );
}

const STATUS_TONES: Record<
  Suggestion["status"],
  "info" | "success" | "critical" | "neutral" | "warning"
> = {
  pending: "info",
  approved: "success",
  published: "success",
  rejected: "neutral",
  superseded: "neutral",
  failed: "critical",
  reverted: "warning",
};

export function SuggestionStatusBadge({
  status,
}: {
  status: Suggestion["status"];
}) {
  return <s-badge tone={STATUS_TONES[status]}>{status}</s-badge>;
}

export function Confidence({ value }: { value: string | null }) {
  if (value === null) return <s-text color="subdued">-</s-text>;
  const pct = Math.round(Number(value) * 100);
  return <s-text>{`${pct}%`}</s-text>;
}

export function FindingTable({
  findings,
  showProduct = false,
}: {
  findings: CatalogFinding[];
  showProduct?: boolean;
}) {
  if (findings.length === 0)
    return <s-paragraph>No findings. Nice work.</s-paragraph>;
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Finding</s-table-header>
        {showProduct ? <s-table-header>Product</s-table-header> : null}
        <s-table-header listSlot="secondary">Severity</s-table-header>
        <s-table-header listSlot="labeled">Why it matters</s-table-header>
        <s-table-header listSlot="labeled">What to do</s-table-header>
        <s-table-header>Evidence</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {findings.map((f) => (
          <s-table-row key={f.id}>
            <s-table-cell>
              <s-stack direction="block" gap="small-300">
                <s-text type="strong">{f.message}</s-text>
                <s-text color="subdued">
                  {`${findingLabel(f.code)}${f.method === "ai" ? " · AI" : ""}${
                    f.confidence !== null
                      ? ` · ${Math.round(Number(f.confidence) * 100)}% confidence`
                      : ""
                  }`}
                </s-text>
              </s-stack>
            </s-table-cell>
            {showProduct ? (
              <s-table-cell>
                <s-link href={`/app/products/${productId(f.product_gid)}`}>
                  {f.product_title || "Untitled product"}
                </s-link>
              </s-table-cell>
            ) : null}
            <s-table-cell>
              <s-badge tone={severityTone(f.severity)}>{f.severity}</s-badge>
            </s-table-cell>
            <s-table-cell>{f.why_it_matters}</s-table-cell>
            <s-table-cell>{f.recommended_action}</s-table-cell>
            <s-table-cell>
              {f.evidence ? (
                `“${f.evidence}”`
              ) : (
                <s-text color="subdued">-</s-text>
              )}
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

const ACTION_LABELS: Record<CatalogChange["action"], string> = {
  approve: "Approved",
  edit_approve: "Edited and approved",
  reject: "Rejected",
  publish: "Written to Shopify",
  publish_failed: "Write failed",
  revert: "Reverted",
};

export function ChangeTable({
  changes,
  onRevert,
  busyId,
  showProduct = true,
}: {
  changes: CatalogChange[];
  onRevert?: (id: number) => void;
  busyId?: number | null;
  showProduct?: boolean;
}) {
  if (changes.length === 0) return <s-paragraph>No changes yet.</s-paragraph>;
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Change</s-table-header>
        {showProduct ? <s-table-header>Product</s-table-header> : null}
        <s-table-header listSlot="labeled">Before</s-table-header>
        <s-table-header listSlot="labeled">After</s-table-header>
        <s-table-header listSlot="secondary">When</s-table-header>
        {onRevert ? <s-table-header>Action</s-table-header> : null}
      </s-table-header-row>
      <s-table-body>
        {changes.map((c) => (
          <s-table-row key={c.id}>
            <s-table-cell>
              <s-stack direction="block" gap="small-300">
                <s-text type="strong">{ACTION_LABELS[c.action]}</s-text>
                <s-text color="subdued">
                  {[
                    c.attribute_key,
                    typeof c.details.metafield === "string"
                      ? c.details.metafield
                      : null,
                    typeof c.details.error === "string"
                      ? c.details.error
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </s-text>
                {c.reverted_at ? (
                  <s-badge tone="warning">Reverted</s-badge>
                ) : null}
              </s-stack>
            </s-table-cell>
            {showProduct ? (
              <s-table-cell>
                <s-link href={`/app/products/${productId(c.product_gid)}`}>
                  {c.product_title || "Untitled product"}
                </s-link>
              </s-table-cell>
            ) : null}
            <s-table-cell>{displayValue(c.before)}</s-table-cell>
            <s-table-cell>{displayValue(c.after)}</s-table-cell>
            <s-table-cell>{formatDate(c.created_at)}</s-table-cell>
            {onRevert ? (
              <s-table-cell>
                {c.revertible ? (
                  <s-button
                    onClick={() => onRevert(c.id)}
                    {...(busyId === c.id ? { loading: true } : {})}
                  >
                    Revert
                  </s-button>
                ) : null}
              </s-table-cell>
            ) : null}
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}
