import { useState } from "react";

import type { ReviewAction, Suggestion } from "../lib/types";
import {
  Confidence,
  editableValue,
  productId,
  SuggestionStatusBadge,
  ValidationBadge,
} from "./Catalog";

const EDITABLE: Suggestion["status"][] = ["pending", "approved", "failed"];

function methodLabel(method: string) {
  const edited = method.includes("merchant_edit") ? " (edited)" : "";
  if (method.startsWith("llm:"))
    return `AI (${method.slice(4).split("+")[0]})${edited}`;
  if (method.startsWith("llm")) return `AI${edited}`;
  if (method.startsWith("rule")) return `Rules${edited}`;
  if (method === "unknown") return "Not found";
  return `${method}${edited}`;
}

/**
 * Suggestions with inline review. Changing the value and approving sends an `edit`, so the
 * merchant's value is validated server-side and logged as an edit, not as the original proposal.
 */
export function SuggestionTable({
  suggestions,
  onReview,
  busy = false,
  selected,
  onSelect,
  showProduct = false,
}: {
  suggestions: Suggestion[];
  onReview: (actions: ReviewAction[]) => void;
  busy?: boolean;
  selected?: Set<string>;
  onSelect?: (id: string, on: boolean) => void;
  showProduct?: boolean;
}) {
  const [edits, setEdits] = useState<Record<string, string>>({});

  if (suggestions.length === 0)
    return <s-paragraph>No suggestions here.</s-paragraph>;

  const approve = (s: Suggestion) => {
    const edited = edits[s.id]?.trim();
    if (edited !== undefined && edited !== editableValue(s)) {
      onReview([{ id: s.id, action: "edit", value: edited }]);
    } else if (s.approved_value !== null && s.approved_value !== undefined) {
      // Re-approving (e.g. retrying a failed write) keeps the value the merchant approved.
      onReview([{ id: s.id, action: "edit", value: editableValue(s) }]);
    } else {
      onReview([{ id: s.id, action: "approve" }]);
    }
  };

  return (
    <s-table variant="auto">
      <s-table-header-row>
        {onSelect ? <s-table-header>Select</s-table-header> : null}
        <s-table-header listSlot="primary">Attribute</s-table-header>
        {showProduct ? <s-table-header>Product</s-table-header> : null}
        <s-table-header listSlot="labeled">Current</s-table-header>
        <s-table-header listSlot="labeled">Suggested</s-table-header>
        <s-table-header listSlot="labeled">Evidence</s-table-header>
        <s-table-header listSlot="secondary">Validation</s-table-header>
        <s-table-header>Action</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {suggestions.map((s) => {
          const editable = EDITABLE.includes(s.status);
          const value = edits[s.id] ?? editableValue(s);
          const missing = !value.trim();
          const unchanged =
            s.status === "approved" && value.trim() === editableValue(s);
          return (
            <s-table-row key={s.id}>
              {onSelect ? (
                <s-table-cell>
                  {editable ? (
                    <s-checkbox
                      accessibilityLabel={`Select ${s.attribute_label}`}
                      checked={selected?.has(s.id) ?? false}
                      onChange={(e) => onSelect(s.id, e.currentTarget.checked)}
                    />
                  ) : null}
                </s-table-cell>
              ) : null}
              <s-table-cell>
                <s-stack direction="block" gap="small-300">
                  <s-text type="strong">{s.attribute_label}</s-text>
                  <s-stack direction="inline" gap="small-300">
                    <SuggestionStatusBadge status={s.status} />
                    <s-text color="subdued">{methodLabel(s.method)}</s-text>
                    <Confidence value={s.confidence} />
                  </s-stack>
                </s-stack>
              </s-table-cell>
              {showProduct ? (
                <s-table-cell>
                  <s-link href={`/app/products/${productId(s.product_gid)}`}>
                    {s.product_title || "Untitled product"}
                  </s-link>
                </s-table-cell>
              ) : null}
              <s-table-cell>{s.display_current || "Not set"}</s-table-cell>
              <s-table-cell>
                {editable ? (
                  <s-text-field
                    label={`Value for ${s.attribute_label}`}
                    labelAccessibilityVisibility="exclusive"
                    value={value}
                    placeholder={
                      s.proposed_value === null
                        ? "Not stated; type a value"
                        : ""
                    }
                    details={
                      s.allowed_values?.length
                        ? `One of: ${s.allowed_values.slice(0, 8).join(", ")}${s.allowed_values.length > 8 ? "…" : ""}`
                        : s.unit
                          ? `Unit: ${s.unit}`
                          : undefined
                    }
                    onInput={(e) =>
                      setEdits({ ...edits, [s.id]: e.currentTarget.value })
                    }
                  />
                ) : (
                  s.display_proposed || "-"
                )}
              </s-table-cell>
              <s-table-cell>
                <s-stack direction="block" gap="small-300">
                  {s.evidence ? (
                    <s-text>{`“${s.evidence}”`}</s-text>
                  ) : (
                    <s-text color="subdued">
                      No evidence in product content
                    </s-text>
                  )}
                  {s.evidence_source ? (
                    <s-text color="subdued">{`From ${s.evidence_source}`}</s-text>
                  ) : null}
                  {s.explanation ? (
                    <s-text color="subdued">{s.explanation}</s-text>
                  ) : null}
                </s-stack>
              </s-table-cell>
              <s-table-cell>
                <s-stack direction="block" gap="small-300">
                  <ValidationBadge suggestion={s} />
                  {[
                    ...s.validation_errors,
                    ...(s.last_error ? [s.last_error] : []),
                  ].map((e) => (
                    <s-text key={e} color="subdued">
                      {e}
                    </s-text>
                  ))}
                </s-stack>
              </s-table-cell>
              <s-table-cell>
                {editable ? (
                  <s-stack direction="inline" gap="small-300">
                    <s-button
                      variant="primary"
                      disabled={busy || missing || unchanged}
                      onClick={() => approve(s)}
                    >
                      {s.status === "approved"
                        ? "Save"
                        : s.status === "failed"
                          ? "Retry"
                          : "Approve"}
                    </s-button>
                    {s.status === "approved" && (
                      <s-button
                        disabled={busy}
                        onClick={() =>
                          onReview([{ id: s.id, action: "reopen" }])
                        }
                      >
                        Back to review
                      </s-button>
                    )}
                    <s-button
                      tone="critical"
                      disabled={busy}
                      onClick={() => onReview([{ id: s.id, action: "reject" }])}
                    >
                      Reject
                    </s-button>
                  </s-stack>
                ) : null}
              </s-table-cell>
            </s-table-row>
          );
        })}
      </s-table-body>
    </s-table>
  );
}
