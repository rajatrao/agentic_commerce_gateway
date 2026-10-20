import { useEffect, useState } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { useFetcher, useLoaderData } from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, errorMessage } from "../lib/backend.server";
import type { AutoApprovalSchema } from "../lib/types";
import { formatDate, productId } from "../components/Catalog";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return { report: await backend.autoApprovalReport(session.shop) };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = form.get("intent");
  const schemaKey = String(form.get("schema_key") ?? "");
  try {
    if (intent === "save") {
      const attributes = form.getAll("attributes").map(String);
      await backend.saveAutoApproval(session.shop, schemaKey, {
        enabled: form.get("enabled") === "true",
        min_confidence: Number(form.get("min_confidence")),
        attributes: form.get("all_attributes") === "true" ? null : attributes,
      });
      return { ok: true, message: "Auto-approval rule saved", error: null };
    }
    if (intent === "apply") {
      const { approved } = await backend.applyAutoApproval(
        session.shop,
        schemaKey,
      );
      return {
        ok: true,
        message: `${approved} pending values approved`,
        error: null,
      };
    }
    const id = String(form.get("suggestion_id"));
    const result = await backend.reviewSuggestions(session.shop, [
      { id, action: "reopen" },
    ]);
    if (result.failed)
      return { ok: false, message: null, error: result.results[0]?.error };
    return { ok: true, message: "Sent back to review", error: null };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

const REASONS: Record<string, string> = {
  "attribute not included": "Attribute not in the rule",
  "no value found": "No value stated in the product",
  "would replace an existing value": "Would replace an existing value",
  "validation warnings": "Has validation warnings",
  "not confirmed by rule-based extraction": "Not confirmed by rule extraction",
};

function reasonLabel(reason: string) {
  return (
    REASONS[reason] ?? reason.replace(/^confidence below/, "Confidence below")
  );
}

function RuleCard({ schema }: { schema: AutoApprovalSchema }) {
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const [enabled, setEnabled] = useState(schema.rule.enabled);
  const [confidence, setConfidence] = useState(schema.rule.min_confidence);
  const [all, setAll] = useState(schema.rule.attributes === null);
  const [chosen, setChosen] = useState<Set<string>>(
    new Set(schema.rule.attributes ?? []),
  );
  const busy =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const save = () => {
    const form = new FormData();
    form.set("intent", "save");
    form.set("schema_key", schema.schema_key);
    form.set("enabled", String(enabled));
    form.set("min_confidence", String(confidence));
    form.set("all_attributes", String(all));
    chosen.forEach((a) => form.append("attributes", a));
    fetcher.submit(form, { method: "POST" });
  };
  const blocked = Object.entries(schema.blocked_by).sort((a, b) => b[1] - a[1]);

  return (
    <s-section heading={schema.schema_name}>
      <s-stack direction="block" gap="base">
        {fetcher.data?.error && (
          <s-banner tone="critical" heading="Not saved">
            <s-paragraph>{fetcher.data.error}</s-paragraph>
          </s-banner>
        )}
        <s-stack direction="inline" gap="base" alignItems="center">
          <s-badge tone={schema.rule.enabled ? "success" : "neutral"}>
            {schema.rule.enabled ? "On" : "Off"}
          </s-badge>
          <s-text>
            {`${schema.qualifying} of ${schema.pending} pending values meet this rule now · ${schema.auto_approved_total} approved automatically so far`}
          </s-text>
        </s-stack>
        {blocked.length > 0 && (
          <s-text color="subdued">
            {`Not qualifying: ${blocked
              .map(([reason, n]) => `${reasonLabel(reason)} (${n})`)
              .join(", ")}`}
          </s-text>
        )}
        <s-checkbox
          label="Approve qualifying values automatically"
          checked={enabled}
          onChange={(e) => setEnabled(e.currentTarget.checked)}
        />
        <s-number-field
          label="Minimum confidence"
          details="Rule-only values score 0.55 to 0.75. Values that AI enrichment and rule extraction both read the same way score higher."
          min={0.5}
          max={1}
          step={0.05}
          value={String(confidence)}
          onChange={(e) => setConfidence(Number(e.currentTarget.value))}
        />
        <s-checkbox
          label="All attributes in this schema"
          checked={all}
          onChange={(e) => setAll(e.currentTarget.checked)}
        />
        {!all && (
          <s-stack direction="inline" gap="small-200">
            {schema.attributes.map((key) => (
              <s-checkbox
                key={key}
                label={key}
                checked={chosen.has(key)}
                onChange={(e) => {
                  const next = new Set(chosen);
                  if (e.currentTarget.checked) next.add(key);
                  else next.delete(key);
                  setChosen(next);
                }}
              />
            ))}
          </s-stack>
        )}
        <s-stack direction="inline" gap="base">
          <s-button
            variant="primary"
            onClick={save}
            {...(busy === "save" ? { loading: true } : {})}
          >
            Save rule
          </s-button>
          <s-button
            disabled={!schema.rule.enabled || schema.qualifying === 0}
            onClick={() =>
              fetcher.submit(
                { intent: "apply", schema_key: schema.schema_key },
                { method: "POST" },
              )
            }
            {...(busy === "apply" ? { loading: true } : {})}
          >
            {`Approve ${schema.qualifying} pending now`}
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

export default function AutoApproval() {
  const { report } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const busyId =
    fetcher.state !== "idle" ? fetcher.formData?.get("suggestion_id") : null;
  const withPending = report.schemas.filter(
    (s) => s.pending > 0 || s.rule.enabled || s.auto_approved_total > 0,
  );
  const quiet = report.schemas.filter((s) => !withPending.includes(s));

  return (
    <s-page heading="Auto-approval">
      <s-section>
        <s-paragraph>
          Off by default. When you turn it on for a category, a suggested value
          is approved without review only if it fills an empty attribute, passed
          validation with no warnings, was read by rule-based extraction from
          the product&apos;s own text, and meets the confidence you set.
          Approved values still wait for you to preview and confirm the write to
          Shopify, and every one is listed below with its evidence.
        </s-paragraph>
      </s-section>

      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Could not send it back">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      {withPending.map((s) => (
        <RuleCard key={s.schema_key} schema={s} />
      ))}
      {quiet.length > 0 && (
        <s-section heading="Other categories">
          <s-paragraph>
            {`${quiet.map((s) => s.schema_name).join(", ")} have no suggestions waiting. Their rules appear here once enrichment produces suggestions for them.`}
          </s-paragraph>
        </s-section>
      )}

      <s-section heading="Approved automatically">
        {report.recent.length === 0 ? (
          <s-paragraph>
            Nothing has been approved automatically yet.
          </s-paragraph>
        ) : (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Product</s-table-header>
              <s-table-header listSlot="labeled">Value</s-table-header>
              <s-table-header listSlot="labeled">Evidence</s-table-header>
              <s-table-header listSlot="secondary">Status</s-table-header>
              <s-table-header>Action</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {report.recent.map((c) => {
                const status = c.suggestion_id
                  ? report.statuses[c.suggestion_id]
                  : undefined;
                const details = c.details as {
                  confidence?: number;
                  evidence?: string | null;
                };
                return (
                  <s-table-row key={c.id}>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-link
                          href={`/app/products/${productId(c.product_gid)}`}
                        >
                          {c.product_title || "Untitled product"}
                        </s-link>
                        <s-text color="subdued">{`${c.attribute_key} · ${formatDate(c.created_at)}`}</s-text>
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-text>{JSON.stringify(c.after)}</s-text>
                        <s-text color="subdued">{`Confidence ${details.confidence ?? "-"}`}</s-text>
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>
                      {details.evidence ? `“${details.evidence}”` : "-"}
                    </s-table-cell>
                    <s-table-cell>
                      <s-badge
                        tone={status === "approved" ? "info" : "neutral"}
                      >
                        {status ?? "unknown"}
                      </s-badge>
                    </s-table-cell>
                    <s-table-cell>
                      {status === "approved" && c.suggestion_id ? (
                        <s-button
                          onClick={() =>
                            fetcher.submit(
                              {
                                intent: "reopen",
                                suggestion_id: c.suggestion_id ?? "",
                              },
                              { method: "POST" },
                            )
                          }
                          {...(busyId === c.suggestion_id
                            ? { loading: true }
                            : {})}
                        >
                          Back to review
                        </s-button>
                      ) : null}
                    </s-table-cell>
                  </s-table-row>
                );
              })}
            </s-table-body>
          </s-table>
        )}
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
