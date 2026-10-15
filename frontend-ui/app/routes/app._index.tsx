import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, BackendError } from "../lib/backend.server";
import {
  DiscoveryScore,
  ProductIssueTable,
  TopIssues,
} from "../components/DiscoveryScore";
import {
  FindingsList,
  InstallationSummary,
} from "../components/InstallationSummary";

const POLL_MS = 3000;

function errorMessage(error: unknown) {
  return error instanceof BackendError
    ? error.detail
    : "The gateway engine is unreachable.";
}

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const [check, summary, worst] = await Promise.allSettled([
    backend.latestInstallationCheck(shop),
    backend.auditSummary(shop),
    backend.auditProducts(shop, { maxScore: 84, limit: 5 }),
  ]);
  const failed = [check, summary, worst].find((r) => r.status === "rejected");
  return {
    shop,
    check: check.status === "fulfilled" ? check.value : null,
    summary: summary.status === "fulfilled" ? summary.value : null,
    worst: worst.status === "fulfilled" ? worst.value : null,
    backendError: failed
      ? errorMessage((failed as PromiseRejectedResult).reason)
      : null,
  };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const intent = (await request.formData()).get("intent");
  try {
    if (intent === "audit") {
      const run = await backend.startAudit(session.shop);
      return {
        intent,
        ok: true,
        message: `Catalog audit ${run.status}`,
        error: null,
      };
    }
    const check = await backend.runInstallationCheck(session.shop);
    return {
      intent,
      ok: true,
      message: check.passed
        ? "Installation check passed"
        : "Installation check finished",
      error: null,
    };
  } catch (error) {
    return { intent, ok: false, message: null, error: errorMessage(error) };
  }
};

export default function Dashboard() {
  const { shop, check, summary, worst, backendError } =
    useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const shopify = useAppBridge();

  const auditRunning = summary?.run?.status === "running";
  const busyIntent =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;
  const error = fetcher.data?.error ?? backendError;

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  // Poll while a background audit is running so the score appears without a manual refresh.
  useEffect(() => {
    if (!auditRunning) return;
    const timer = setInterval(() => {
      if (revalidator.state === "idle") revalidator.revalidate();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [auditRunning, revalidator]);

  const submit = (intent: "audit" | "installation") =>
    fetcher.submit({ intent }, { method: "POST" });

  return (
    <s-page heading="Agentic Commerce Gateway">
      <s-button
        slot="primary-action"
        variant="primary"
        onClick={() => submit("audit")}
        {...(busyIntent === "audit" || auditRunning ? { loading: true } : {})}
      >
        Run catalog audit
      </s-button>
      <s-button
        slot="secondary-actions"
        onClick={() => submit("installation")}
        {...(busyIntent === "installation" ? { loading: true } : {})}
      >
        Run installation check
      </s-button>

      {error && (
        <s-banner tone="critical" heading="Could not reach the gateway engine">
          <s-paragraph>{error}</s-paragraph>
        </s-banner>
      )}
      {summary?.run?.status === "failed" && (
        <s-banner tone="warning" heading="The last catalog audit failed">
          <s-paragraph>
            The engine could not read your catalog from Shopify. Try again; if
            it keeps failing, reinstall the app to refresh its permissions.
          </s-paragraph>
        </s-banner>
      )}

      <s-section heading="AI Discovery Score">
        {auditRunning && (
          <s-banner tone="info" heading="Audit in progress">
            <s-paragraph>{`${summary?.run?.products_scanned ?? 0} products scored so far.`}</s-paragraph>
          </s-banner>
        )}
        {summary ? <DiscoveryScore summary={summary} /> : null}
      </s-section>

      {worst && worst.total > 0 && (
        <s-section heading="Products that need structured data">
          <s-stack direction="block" gap="base">
            <ProductIssueTable products={worst.items} />
            <s-link href="/app/products?max_score=84">{`View all ${worst.total} products`}</s-link>
          </s-stack>
        </s-section>
      )}

      <s-section heading="Storefront readiness">
        {check ? (
          <InstallationSummary check={check} />
        ) : (
          <s-paragraph>
            No installation check has run for {shop} yet. Run one to see whether
            your theme exposes valid JSON-LD, page metadata and the
            gateway&apos;s app block.
          </s-paragraph>
        )}
      </s-section>

      {check && check.details.findings.length > 0 && (
        <s-section heading="Storefront findings">
          <FindingsList findings={check.details.findings} />
        </s-section>
      )}

      <s-section slot="aside" heading="Most common gaps">
        {summary && summary.top_issues.length > 0 ? (
          <TopIssues summary={summary} />
        ) : (
          <s-paragraph>
            Run a catalog audit to see what to fix first.
          </s-paragraph>
        )}
      </s-section>

      <s-section slot="aside" heading="Agent feed">
        <s-paragraph>
          Your catalog is published for AI agents as llm.txt. Add brand FAQs to
          it from the gateway API; a settings page is coming next.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
