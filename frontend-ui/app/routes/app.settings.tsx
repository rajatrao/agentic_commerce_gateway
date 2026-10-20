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
import { COMPONENT_LABELS } from "../components/Catalog";

const COMPONENT_HELP: Record<string, string> = {
  completeness:
    "Share of the category's attributes with a valid structured value, weighted by importance (critical 4, high 3, medium 2, low 1).",
  validity:
    "Share of structured values that parse and fit the schema: allowed values, units, number ranges.",
  consistency:
    "Share of structured values that agree with what the description says. Skipped when there is nothing to compare.",
  evidence:
    "Share of claims such as waterproof or recycled that are backed by a structured field. Skipped when there are no claims.",
  variants:
    "Variant checks: SKUs, prices, named options and the options the category expects (such as Size).",
  foundation:
    "The existing AI Discovery Score: title, description, media, identifiers, offers and taxonomy.",
};

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return { settings: await backend.catalogSettings(session.shop) };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const weights: Record<string, number> = {};
  for (const key of Object.keys(COMPONENT_LABELS)) {
    weights[key] = Number(form.get(`weight_${key}`) ?? 0);
  }
  try {
    const settings = await backend.saveCatalogSettings(session.shop, {
      readiness_weights: weights,
      ready_threshold: Number(form.get("ready_threshold")),
    });
    return {
      ok: true,
      message: "Settings saved. The next audit uses them.",
      error: null,
      settings,
    };
  } catch (error) {
    return {
      ok: false,
      message: null,
      error: errorMessage(error),
      settings: null,
    };
  }
};

export default function Settings() {
  const { settings: loaded } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const settings = fetcher.data?.settings ?? loaded;
  const [weights, setWeights] = useState(settings.readiness_weights);
  const [threshold, setThreshold] = useState(settings.ready_threshold);
  const busy = fetcher.state !== "idle";

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const total = Object.values(weights).reduce((a, b) => a + (b || 0), 0);

  return (
    <s-page heading="Settings">
      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Settings were not saved">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      <fetcher.Form method="post">
        <s-section heading="Readiness score">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Each product&apos;s readiness score is a weighted average of the
              components below, each scored 0 to 100%. Components that do not
              apply to a product (for example consistency, when nothing
              structured can be compared with the description) are left out and
              the rest are rescaled, so every product is scored out of 100.
              Every lost point traces back to a finding on the product.
            </s-paragraph>
            <s-table variant="auto">
              <s-table-header-row>
                <s-table-header listSlot="primary">Component</s-table-header>
                <s-table-header listSlot="labeled">
                  How it is measured
                </s-table-header>
                <s-table-header listSlot="secondary">Weight</s-table-header>
                <s-table-header format="numeric">Share</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {Object.entries(COMPONENT_LABELS).map(([key, label]) => (
                  <s-table-row key={key}>
                    <s-table-cell>{label}</s-table-cell>
                    <s-table-cell>{COMPONENT_HELP[key]}</s-table-cell>
                    <s-table-cell>
                      <s-number-field
                        label={`${label} weight`}
                        labelAccessibilityVisibility="exclusive"
                        name={`weight_${key}`}
                        min={0}
                        max={100}
                        step={1}
                        value={String(weights[key] ?? 0)}
                        details={`Default ${settings.default_weights[key]}`}
                        onInput={(e) =>
                          setWeights({
                            ...weights,
                            [key]: Number(e.currentTarget.value) || 0,
                          })
                        }
                      />
                    </s-table-cell>
                    <s-table-cell>
                      {total
                        ? `${Math.round(((weights[key] ?? 0) / total) * 100)}%`
                        : "-"}
                    </s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
            <s-number-field
              label="Agent-ready threshold"
              name="ready_threshold"
              min={1}
              max={100}
              value={String(threshold)}
              details="Products at or above this score count as agent-ready on the dashboard."
              onInput={(e) => setThreshold(Number(e.currentTarget.value) || 0)}
            />
            <s-stack direction="inline" gap="base">
              <s-button
                type="submit"
                variant="primary"
                disabled={total === 0}
                {...(busy ? { loading: true } : {})}
              >
                Save
              </s-button>
              <s-button
                onClick={() => {
                  setWeights(settings.default_weights);
                  setThreshold(80);
                }}
              >
                Restore defaults
              </s-button>
            </s-stack>
          </s-stack>
        </s-section>
      </fetcher.Form>

      <s-section heading="AI enrichment">
        <s-stack direction="block" gap="small-200">
          {settings.ai_enabled ? (
            <s-badge tone="success">{`Claude enabled (${settings.ai_model})`}</s-badge>
          ) : (
            <s-badge tone="neutral">Rules only</s-badge>
          )}
          <s-paragraph>
            {settings.ai_enabled
              ? "Suggestions combine deterministic rules with Claude. Claude only sees the product's own content, treats it as data, and must quote the text behind every value. The engine checks each quote against the product before showing it."
              : "Suggestions come from deterministic rules that read the product's own title, description, tags and options. Set ANTHROPIC_API_KEY on the gateway engine to also use Claude."}
          </s-paragraph>
        </s-stack>
      </s-section>

      <s-section heading="Publishing">
        <s-stack direction="block" gap="small-200">
          <s-badge tone="info">Review required</s-badge>
          <s-paragraph>{settings.auto_publish_note}</s-paragraph>
        </s-stack>
      </s-section>

      <s-section slot="aside" heading="Related settings">
        <s-unordered-list>
          <s-list-item>
            <s-link href="/app/schemas">Category schemas</s-link>
          </s-list-item>
          <s-list-item>
            <s-link href="/app/mappings">Metafield mapping</s-link>
          </s-list-item>
        </s-unordered-list>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
