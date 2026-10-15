import { useEffect } from "react";
import type { ActionFunctionArgs, HeadersFunction, LoaderFunctionArgs } from "react-router";
import { useFetcher, useLoaderData } from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, BackendError } from "../lib/backend.server";
import type { Finding, InstallationCheck, Severity } from "../lib/types";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  try {
    const check = await backend.latestInstallationCheck(session.shop);
    return { shop: session.shop, check, backendError: null as string | null };
  } catch (error) {
    const message = error instanceof BackendError ? error.detail : "Backend unreachable";
    return { shop: session.shop, check: null, backendError: message };
  }
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const storeUrl = (form.get("storeUrl") as string | null)?.trim() || undefined;
  try {
    const check = await backend.runInstallationCheck(session.shop, storeUrl);
    return { check, error: null as string | null };
  } catch (error) {
    const message = error instanceof BackendError ? error.detail : "Backend unreachable";
    return { check: null, error: message };
  }
};

const SEVERITY_TONE: Record<Severity, "critical" | "warning" | "info"> = {
  error: "critical",
  warning: "warning",
  info: "info",
};

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return <s-badge tone={ok ? "success" : "critical"}>{`${label}: ${ok ? "Yes" : "No"}`}</s-badge>;
}

function FindingsList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return <s-paragraph>No issues found. Your storefront is ready for AI agents.</s-paragraph>;
  }
  return (
    <s-stack direction="block" gap="base">
      {findings.map((finding, index) => (
        <s-banner
          key={`${finding.code}-${index}`}
          tone={SEVERITY_TONE[finding.severity]}
          heading={finding.message}
        >
          {finding.fix && <s-paragraph>{finding.fix}</s-paragraph>}
          {finding.page && <s-text color="subdued">{finding.page}</s-text>}
        </s-banner>
      ))}
    </s-stack>
  );
}

function CheckSummary({ check }: { check: InstallationCheck }) {
  const { details } = check;
  return (
    <s-stack direction="block" gap="base">
      <s-stack direction="inline" gap="small-200">
        <StatusBadge ok={check.jsonld_valid} label="Valid JSON-LD" />
        <StatusBadge ok={check.has_product_schema} label="Product schema" />
        <StatusBadge ok={check.has_org_schema} label="Organization schema" />
        <StatusBadge ok={check.app_block_enabled} label="App block active" />
        <StatusBadge ok={details.ai_crawlers_blocked.length === 0} label="AI crawlers allowed" />
        <StatusBadge ok={details.llms_txt_present} label="llms.txt" />
      </s-stack>
      <s-paragraph>
        <s-text type="strong">Metadata coverage: </s-text>
        {Math.round(details.metadata_coverage * 100)}%
        {" · "}
        <s-text type="strong">Schemas found: </s-text>
        {check.schemas_found.length ? check.schemas_found.join(", ") : "none"}
      </s-paragraph>
      <s-text color="subdued">
        Checked {new Date(check.checked_at).toLocaleString()} against {check.store_url}
      </s-text>
    </s-stack>
  );
}

export default function Dashboard() {
  const { shop, check: initialCheck, backendError } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();

  const running = fetcher.state !== "idle";
  const check = fetcher.data?.check ?? initialCheck;
  const error = fetcher.data?.error ?? backendError;

  useEffect(() => {
    if (fetcher.data?.check) {
      shopify.toast.show(
        fetcher.data.check.passed ? "Installation check passed" : "Installation check finished",
      );
    }
  }, [fetcher.data?.check, shopify]);

  const runCheck = () => fetcher.submit({}, { method: "POST" });

  return (
    <s-page heading="Agentic Commerce Gateway">
      <s-button
        slot="primary-action"
        variant="primary"
        onClick={runCheck}
        {...(running ? { loading: true } : {})}
      >
        Run installation check
      </s-button>

      {error && (
        <s-banner tone="critical" heading="Could not reach the gateway engine">
          <s-paragraph>{error}</s-paragraph>
        </s-banner>
      )}

      <s-section heading="Storefront readiness">
        {check ? (
          <s-stack direction="block" gap="base">
            <s-banner
              tone={check.passed ? "success" : "warning"}
              heading={
                check.passed
                  ? "AI agents can discover and read your catalog"
                  : "Your storefront needs changes before AI agents can use it"
              }
            />
            <CheckSummary check={check} />
          </s-stack>
        ) : (
          <s-paragraph>
            No installation check has run for {shop} yet. Run one to see whether your theme
            exposes valid JSON-LD, page metadata and the gateway&apos;s app block.
          </s-paragraph>
        )}
      </s-section>

      {check && (
        <s-section heading="Findings">
          <FindingsList findings={check.details.findings} />
        </s-section>
      )}

      <s-section slot="aside" heading="Coming next">
        <s-unordered-list>
          <s-list-item>AI Discovery Score for every product</s-list-item>
          <s-list-item>llm.txt feed for agents</s-list-item>
          <s-list-item>UCP, ACP and MCP checkout adapters</s-list-item>
        </s-unordered-list>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
