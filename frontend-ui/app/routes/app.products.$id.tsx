import { useEffect, useState } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, errorMessage } from "../lib/backend.server";
import type { ReviewAction } from "../lib/types";
import {
  ChangeTable,
  COMPONENT_LABELS,
  FindingTable,
  ReadinessBadge,
  severityTone,
} from "../components/Catalog";
import { SuggestionTable } from "../components/SuggestionTable";

const POLL_MS = 3000;

export const loader = async ({ request, params }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const id = params.id ?? "";
  const [detail, settings, jobs] = await Promise.all([
    backend.catalogProduct(session.shop, id),
    backend.catalogSettings(session.shop),
    backend.enrichmentJobs(session.shop),
  ]);
  if (!detail) throw new Response("Product not found", { status: 404 });
  const gid = detail.product.product_gid;
  const activeJob = jobs.find(
    (j) => j.status === "queued" || j.status === "running",
  );
  const job = activeJob
    ? await backend.enrichmentJob(session.shop, activeJob.id)
    : null;
  const running =
    !!job && !!job.product_gids?.includes(gid) && !(gid in job.results);
  return {
    id,
    detail,
    aiEnabled: settings.ai_enabled,
    threshold: settings.ready_threshold,
    running,
  };
};

export const action = async ({ request, params }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = form.get("intent");
  try {
    if (intent === "enrich") {
      await backend.startEnrichment(session.shop, {
        product_gids: [`gid://shopify/Product/${params.id}`],
        use_ai: form.get("use_ai") === "true",
      });
      return { ok: true, message: "Generating suggestions", error: null };
    }
    if (intent === "revert") {
      await backend.revertChange(session.shop, Number(form.get("change_id")));
      return { ok: true, message: "Change reverted in Shopify", error: null };
    }
    const actions = JSON.parse(String(form.get("actions"))) as ReviewAction[];
    const result = await backend.reviewSuggestions(session.shop, actions);
    const errors = result.results.filter((r) => !r.ok).map((r) => r.error);
    return {
      ok: result.failed === 0,
      message: result.failed === 0 ? "Review saved" : null,
      error: errors.length ? errors.join("; ") : null,
    };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

export default function ProductDetail() {
  const { id, detail, aiEnabled, threshold, running } =
    useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const shopify = useAppBridge();
  const [useAi, setUseAi] = useState(aiEnabled);
  const busyIntent =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;
  const { product, suggestions } = detail;

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      if (revalidator.state === "idle") revalidator.revalidate();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [running, revalidator]);

  const review = (actions: ReviewAction[]) =>
    fetcher.submit(
      { intent: "review", actions: JSON.stringify(actions) },
      { method: "POST" },
    );

  const open = suggestions.filter((s) =>
    ["pending", "approved", "failed"].includes(s.status),
  );
  const approved = suggestions.filter((s) => s.status === "approved").length;
  const published = suggestions.filter((s) => s.status === "published");
  const breakdown = Object.entries(detail.readiness_breakdown);

  return (
    <s-page heading={product.title || "Untitled product"}>
      <s-link slot="breadcrumb-actions" href="/app/products">
        Catalog audit
      </s-link>
      <s-button
        slot="primary-action"
        variant="primary"
        onClick={() =>
          fetcher.submit(
            { intent: "enrich", use_ai: String(useAi) },
            { method: "POST" },
          )
        }
        {...(busyIntent === "enrich" || running ? { loading: true } : {})}
      >
        Generate suggestions
      </s-button>
      <s-button
        slot="secondary-actions"
        href={product.admin_url}
        target="_blank"
      >
        Open in Shopify admin
      </s-button>

      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Something needs your attention">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}
      {running && (
        <s-banner tone="info" heading="Generating suggestions">
          <s-paragraph>
            Reading this product&apos;s content. Suggestions appear here when
            they are ready.
          </s-paragraph>
        </s-banner>
      )}

      <s-section heading="Suggested attribute values">
        <s-stack direction="block" gap="base">
          <s-paragraph>
            Every value comes from this product&apos;s own title, description,
            tags or options, with the quote that states it. Values that are not
            stated stay empty for you to fill in. Nothing is written to Shopify
            until you approve it and confirm the write.
          </s-paragraph>
          <s-checkbox
            label="Use Claude when generating suggestions"
            checked={useAi}
            disabled={!aiEnabled}
            onChange={(e) => setUseAi(e.currentTarget.checked)}
          />
          <SuggestionTable
            suggestions={open}
            onReview={review}
            busy={busyIntent === "review"}
          />
          {approved > 0 && (
            <s-link href={`/app/review?status=approved&product=${id}`}>
              {`Preview and write ${approved} approved value${approved === 1 ? "" : "s"} to Shopify`}
            </s-link>
          )}
        </s-stack>
      </s-section>

      <s-section heading="Attributes">
        {detail.attributes.length === 0 ? (
          <s-paragraph>
            This product does not match a category schema yet. Add its product
            type to a schema on the{" "}
            <s-link href="/app/schemas">Category schemas</s-link> page.
          </s-paragraph>
        ) : (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Attribute</s-table-header>
              <s-table-header listSlot="secondary">Importance</s-table-header>
              <s-table-header listSlot="labeled">Current value</s-table-header>
              <s-table-header>Source</s-table-header>
              <s-table-header>Metafield</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {detail.attributes.map((a) => (
                <s-table-row key={a.key}>
                  <s-table-cell>
                    <s-stack direction="block" gap="small-300">
                      <s-text type="strong">{a.label}</s-text>
                      <s-text color="subdued">{`${a.type}${a.unit ? ` (${a.unit})` : ""}${a.required ? " · required" : ""}`}</s-text>
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>
                    <s-badge tone={severityTone(a.importance)}>
                      {a.importance}
                    </s-badge>
                  </s-table-cell>
                  <s-table-cell>
                    <s-stack direction="block" gap="small-300">
                      <s-text>{a.current_display}</s-text>
                      {a.errors.map((e) => (
                        <s-text key={e} tone="critical">
                          {e}
                        </s-text>
                      ))}
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>{a.source ?? "-"}</s-table-cell>
                  <s-table-cell>{a.metafield ?? "-"}</s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-section>

      <s-section heading="Findings">
        <FindingTable findings={detail.findings} />
      </s-section>

      <s-section heading="Change history">
        <ChangeTable
          changes={detail.changes}
          showProduct={false}
          busyId={
            busyIntent === "revert"
              ? Number(fetcher.formData?.get("change_id"))
              : null
          }
          onRevert={(changeId) =>
            fetcher.submit(
              { intent: "revert", change_id: String(changeId) },
              { method: "POST" },
            )
          }
        />
      </s-section>

      <s-section slot="aside" heading="Agent readiness">
        <s-stack direction="block" gap="base">
          <s-stack direction="inline" gap="base" alignItems="center">
            <ReadinessBadge
              score={product.readiness_score}
              threshold={threshold}
            />
            <s-text color="subdued">
              {product.readiness_score !== null &&
              product.readiness_score >= threshold
                ? "Agent-ready"
                : `Needs ${threshold} to count as agent-ready`}
            </s-text>
          </s-stack>
          <s-text color="subdued">{`Category: ${detail.schema_name ?? "none"}${product.category_name ? ` · Shopify category: ${product.category_name}` : ""}`}</s-text>
          {breakdown.length > 0 && (
            <s-unordered-list>
              {breakdown.map(([key, c]) => (
                <s-list-item key={key}>
                  {`${COMPONENT_LABELS[key] ?? key}: ${
                    c.applicable && c.value !== null
                      ? `${Math.round(c.value * 100)}% (weight ${c.weight})`
                      : "not applicable"
                  }`}
                </s-list-item>
              ))}
            </s-unordered-list>
          )}
          <s-link href="/app/settings">How the score is calculated</s-link>
        </s-stack>
      </s-section>

      {published.length > 0 && (
        <s-section slot="aside" heading="Written to Shopify">
          <s-unordered-list>
            {published.map((s) => (
              <s-list-item key={s.id}>
                {`${s.attribute_label}: ${s.display_proposed}`}
              </s-list-item>
            ))}
          </s-unordered-list>
        </s-section>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
