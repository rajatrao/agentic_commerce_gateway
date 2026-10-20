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
import { backend, errorMessage } from "../lib/backend.server";
import type {
  CatalogJob,
  CatalogOverview,
  DiscoveryRunResults,
  RunPoint,
} from "../lib/types";
import { DiscoveryScore, TopIssues } from "../components/DiscoveryScore";
import {
  FindingsList,
  InstallationSummary,
} from "../components/InstallationSummary";
import {
  ChangeTable,
  findingLabel,
  formatDate,
  readinessTone,
  severityTone,
} from "../components/Catalog";

const POLL_MS = 3000;

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const [check, summary, overview] = await Promise.allSettled([
    backend.latestInstallationCheck(shop),
    backend.auditSummary(shop),
    backend.catalogOverview(shop),
  ]);
  const failed = [check, summary, overview].find(
    (r) => r.status === "rejected",
  );
  return {
    shop,
    check: check.status === "fulfilled" ? check.value : null,
    summary: summary.status === "fulfilled" ? summary.value : null,
    overview: overview.status === "fulfilled" ? overview.value : null,
    backendError: failed
      ? errorMessage((failed as PromiseRejectedResult).reason)
      : null,
  };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = form.get("intent");
  try {
    if (intent === "audit") {
      const run = await backend.startAudit(session.shop);
      return { ok: true, message: `Catalog audit ${run.status}`, error: null };
    }
    if (intent === "discovery") {
      await backend.startDiscoveryRun(session.shop);
      return {
        ok: true,
        message: "Asking your store's search the shopper questions",
        error: null,
      };
    }
    if (intent === "enrich") {
      const job = await backend.startEnrichment(session.shop, {
        lowest_readiness: 25,
        use_ai: form.get("use_ai") === "true",
      });
      return {
        ok: true,
        message: `Generating suggestions for ${job.total} products`,
        error: null,
      };
    }
    const check = await backend.runInstallationCheck(session.shop);
    return {
      ok: true,
      message: check.passed
        ? "Installation check passed"
        : "Installation check finished",
      error: null,
    };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

function searchResults(run: CatalogJob | null) {
  if (!run || run.status !== "completed") return null;
  return run.results as unknown as DiscoveryRunResults;
}

const SEARCH_LABEL: Record<DiscoveryRunResults["method"], string> = {
  storefront_mcp: "your store's Storefront MCP search, the tool AI agents call",
  admin_search: "Shopify's product search (keyword based)",
};

function DiscoverySection({
  overview,
  running,
  onRun,
}: {
  overview: CatalogOverview;
  running: boolean;
  onRun: () => void;
}) {
  const results = searchResults(overview.discovery_run);
  const byQuery = new Map(
    (results?.queries ?? []).map((q) => [`${q.schema_key}:${q.query}`, q]),
  );
  const failedRun = overview.discovery_run?.status === "failed";
  return (
    <s-section heading="Can agents answer these shopper questions?">
      <s-stack direction="block" gap="base">
        <s-paragraph>
          Each question needs specific attributes. A product can answer it when
          every one of them is filled in with a valid value. The store search
          test asks your store each question the way an agent does and checks
          whether those products come back.
        </s-paragraph>
        <s-stack direction="inline" gap="base" alignItems="center">
          <s-button
            onClick={onRun}
            {...(running ? { loading: true } : {})}
            disabled={!overview.has_audit}
          >
            Test with store search
          </s-button>
          {results && (
            <s-text color="subdued">
              {`Tested ${formatDate(overview.discovery_run?.finished_at ?? null)} with ${SEARCH_LABEL[results.method]}.${
                results.avg_recall !== null
                  ? ` On average ${Math.round(results.avg_recall * 100)}% of the products that can answer a question were in the top ${results.result_limit} results.`
                  : ""
              }`}
            </s-text>
          )}
        </s-stack>
        {results?.fallback_reason && (
          <s-banner tone="info" heading="Storefront search was not reachable">
            <s-paragraph>
              {`The test used Shopify's keyword product search instead (${results.fallback_reason}). Password-protected development stores block the storefront search agents use.`}
            </s-paragraph>
          </s-banner>
        )}
        {failedRun && (
          <s-banner tone="warning" heading="The store search test failed">
            <s-paragraph>
              {overview.discovery_run?.last_error ?? "Unknown error"}
            </s-paragraph>
          </s-banner>
        )}
        <s-table variant="auto">
          <s-table-header-row>
            <s-table-header listSlot="primary">Shopper question</s-table-header>
            <s-table-header>Needs</s-table-header>
            <s-table-header listSlot="secondary" format="numeric">
              Products that can answer
            </s-table-header>
            <s-table-header listSlot="labeled">
              Found by store search
            </s-table-header>
          </s-table-header-row>
          <s-table-body>
            {overview.discovery_tests.map((t) => {
              const pct = t.products
                ? Math.round((t.supported / t.products) * 100)
                : 0;
              const found = byQuery.get(`${t.schema_key}:${t.query}`);
              return (
                <s-table-row key={`${t.schema_key}:${t.query}`}>
                  <s-table-cell>
                    <s-stack direction="block" gap="small-300">
                      <s-text>{`“${t.query}”`}</s-text>
                      <s-text color="subdued">{t.schema_name}</s-text>
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>{t.attributes.join(", ")}</s-table-cell>
                  <s-table-cell>
                    <s-badge tone={readinessTone(pct)}>
                      {`${t.supported} of ${t.products}`}
                    </s-badge>
                  </s-table-cell>
                  <s-table-cell>
                    {found ? (
                      <s-stack direction="block" gap="small-300">
                        {found.error ? (
                          <s-badge tone="warning">Search failed</s-badge>
                        ) : found.recall === null ? (
                          <s-text color="subdued">
                            {`No product can answer it yet; ${found.returned} results, ${found.in_schema} in this category`}
                          </s-text>
                        ) : (
                          <>
                            <s-badge
                              tone={readinessTone(
                                Math.round(found.recall * 100),
                              )}
                            >
                              {`${found.answerable_found} of ${Math.min(found.answerable_products, results?.result_limit ?? 10)} found`}
                            </s-badge>
                            <s-text color="subdued">
                              {`${found.returned} results, ${found.in_schema} in this category`}
                            </s-text>
                          </>
                        )}
                        {found.missed_examples.length > 0 && (
                          <s-text color="subdued">
                            {`Missed: ${found.missed_examples.slice(0, 3).join(", ")}`}
                          </s-text>
                        )}
                      </s-stack>
                    ) : (
                      <s-text color="subdued">Not tested yet</s-text>
                    )}
                  </s-table-cell>
                </s-table-row>
              );
            })}
          </s-table-body>
        </s-table>
      </s-stack>
    </s-section>
  );
}

function isActive(status: string | undefined) {
  return status === "queued" || status === "running";
}

export default function Dashboard() {
  const { shop, check, summary, overview, backendError } =
    useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const shopify = useAppBridge();

  // Queued runs wait for a backend worker; treat them as in progress too.
  const auditRunning = isActive(summary?.run?.status);
  const enrichRunning = isActive(overview?.recent_jobs[0]?.status);
  const discoveryRunning = isActive(overview?.discovery_run?.status);
  const busyIntent =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;
  const error = fetcher.data?.error ?? backendError;

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  // Poll while background work runs so results appear without a manual refresh.
  useEffect(() => {
    if (!auditRunning && !enrichRunning && !discoveryRunning) return;
    const timer = setInterval(() => {
      if (revalidator.state === "idle") revalidator.revalidate();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [auditRunning, enrichRunning, discoveryRunning, revalidator]);

  const submit = (intent: string, extra: Record<string, string> = {}) =>
    fetcher.submit({ intent, ...extra }, { method: "POST" });

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
        onClick={() =>
          submit("enrich", { use_ai: String(overview?.ai_enabled ?? false) })
        }
        disabled={!overview?.has_audit}
        {...(busyIntent === "enrich" || enrichRunning ? { loading: true } : {})}
      >
        Enrich 25 lowest-scoring products
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
      {auditRunning && (
        <s-banner tone="info" heading="Audit in progress">
          <s-paragraph>{`${summary?.run?.products_scanned ?? 0} products scored so far.`}</s-paragraph>
        </s-banner>
      )}

      {overview && <SetupGuide overview={overview} hasCheck={!!check} />}

      {overview?.has_audit && <Metrics overview={overview} />}

      {overview && overview.trend.length > 1 && (
        <s-section heading="Readiness over time">
          <Trend runs={overview.trend} />
        </s-section>
      )}

      {overview && overview.discovery_tests.length > 0 && (
        <DiscoverySection
          overview={overview}
          running={discoveryRunning || busyIntent === "discovery"}
          onRun={() => submit("discovery")}
        />
      )}

      {overview && overview.top_findings.length > 0 && (
        <s-section heading="What to fix first">
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Finding</s-table-header>
              <s-table-header listSlot="secondary">Severity</s-table-header>
              <s-table-header format="numeric">Count</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {overview.top_findings.slice(0, 8).map((f) => (
                <s-table-row key={f.code}>
                  <s-table-cell>
                    <s-link
                      href={`/app/products?code=${encodeURIComponent(f.code)}`}
                    >
                      {findingLabel(f.code)}
                    </s-link>
                  </s-table-cell>
                  <s-table-cell>
                    <s-badge tone={severityTone(f.severity)}>
                      {f.severity}
                    </s-badge>
                  </s-table-cell>
                  <s-table-cell>{String(f.count)}</s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        </s-section>
      )}

      {overview && overview.recent_changes.length > 0 && (
        <s-section heading="Recent changes">
          <s-stack direction="block" gap="base">
            <ChangeTable changes={overview.recent_changes.slice(0, 5)} />
            <s-link href="/app/activity">See all activity</s-link>
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

      <s-section slot="aside" heading="AI Discovery Score">
        {summary ? <DiscoveryScore summary={summary} /> : null}
      </s-section>

      <s-section slot="aside" heading="Most common gaps">
        {summary && summary.top_issues.length > 0 ? (
          <TopIssues summary={summary} />
        ) : (
          <s-paragraph>
            Run a catalog audit to see what to fix first.
          </s-paragraph>
        )}
      </s-section>

      <s-section slot="aside" heading="Recent activity">
        {overview ? <RecentActivity overview={overview} /> : null}
      </s-section>
    </s-page>
  );
}

function SetupGuide({
  overview,
  hasCheck,
}: {
  overview: CatalogOverview;
  hasCheck: boolean;
}) {
  const s = overview.suggestions;
  const steps = [
    {
      done: overview.has_audit,
      label: "Audit your catalog",
      detail:
        "Scores every active product against its category schema and lists what is missing.",
      href: null,
    },
    {
      done: overview.recent_jobs.length > 0,
      label: "Generate attribute suggestions",
      detail: overview.ai_enabled
        ? "Reads each product's own text with rules and AI, keeping the quote behind every value."
        : "Reads each product's own text with rules. Configure an AI provider on the engine to also use AI.",
      href: "/app/products",
    },
    {
      done: (s.approved ?? 0) + (s.published ?? 0) > 0,
      label: "Review suggestions",
      detail:
        "Approve, edit or reject each value. Nothing is written until you do.",
      href: "/app/review",
    },
    {
      done: (s.published ?? 0) > 0,
      label: "Write approved values to Shopify",
      detail:
        "Preview the metafields first. Every write is logged and can be reverted.",
      href: "/app/review?status=approved",
    },
    {
      done: hasCheck,
      label: "Check your storefront",
      detail: "Confirms your theme exposes the data to agents and search.",
      href: null,
    },
  ];
  if (steps.every((step) => step.done)) return null;
  const done = steps.filter((step) => step.done).length;
  return (
    <s-section heading={`Setup guide (${done} of ${steps.length} done)`}>
      <s-stack direction="block" gap="base">
        {steps.map((step) => (
          <s-stack
            key={step.label}
            direction="inline"
            gap="base"
            alignItems="center"
          >
            <s-icon
              type={step.done ? "check-circle-filled" : "circle-dashed"}
              tone={step.done ? "success" : "neutral"}
            />
            <s-stack direction="block" gap="small-300">
              {step.href && !step.done ? (
                <s-link href={step.href}>{step.label}</s-link>
              ) : (
                <s-text type="strong">{step.label}</s-text>
              )}
              <s-text color="subdued">{step.detail}</s-text>
            </s-stack>
          </s-stack>
        ))}
      </s-stack>
    </s-section>
  );
}

function Metric({
  label,
  value,
  href,
  tone,
  help,
}: {
  label: string;
  value: string;
  href?: string;
  tone?: "success" | "info" | "warning" | "critical" | "neutral";
  help?: string;
}) {
  return (
    <s-box padding="base" border="base" borderRadius="base">
      <s-stack direction="block" gap="small-300">
        {href ? (
          <s-link href={href}>{label}</s-link>
        ) : (
          <s-text color="subdued">{label}</s-text>
        )}
        {tone ? (
          <s-badge tone={tone} size="large">
            {value}
          </s-badge>
        ) : (
          <s-heading>{value}</s-heading>
        )}
        {help ? <s-text color="subdued">{help}</s-text> : null}
      </s-stack>
    </s-box>
  );
}

function Metrics({ overview }: { overview: CatalogOverview }) {
  const readiness =
    overview.readiness_score === null
      ? null
      : Math.round(Number(overview.readiness_score));
  const sev = overview.findings_by_severity;
  const highPriority = (sev.critical ?? 0) + (sev.high ?? 0);
  const s = overview.suggestions;
  return (
    <s-section heading="Catalog readiness">
      <s-grid
        gridTemplateColumns="repeat(auto-fill, minmax(180px, 1fr))"
        gap="base"
      >
        <Metric
          label="Readiness score"
          value={readiness === null ? "-" : `${readiness} / 100`}
          tone={readinessTone(readiness, overview.ready_threshold)}
          help="Average over analyzed products"
        />
        <Metric
          label="Products analyzed"
          value={String(overview.products_analyzed)}
          href="/app/products"
        />
        <Metric
          label="Agent-ready products"
          value={`${overview.ready_products} of ${overview.products_analyzed}`}
          help={`Readiness ${overview.ready_threshold} or more`}
        />
        <Metric
          label="Missing required attributes"
          value={String(overview.products_missing_required)}
          href="/app/products?code=attribute_missing"
          help={`${overview.missing_attribute_instances} gaps in total`}
        />
        <Metric
          label="Critical and high findings"
          value={String(highPriority)}
          href="/app/products?severity=critical,high"
        />
        <Metric
          label="Suggestions to review"
          value={String(s.pending ?? 0)}
          href="/app/review"
        />
        <Metric
          label="Approved, not yet written"
          value={String(s.approved ?? 0)}
          href="/app/review?status=approved"
        />
        <Metric
          label="Failed validation"
          value={String(overview.failed_validations + (s.failed ?? 0))}
          href="/app/review?validation=invalid"
          help="Invalid suggestions and failed writes"
        />
      </s-grid>
    </s-section>
  );
}

function score(value: string | null) {
  return value === null ? null : Math.round(Number(value));
}

function Trend({ runs }: { runs: RunPoint[] }) {
  const points = runs.filter((r) => r.status === "completed").slice(-8);
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Audit</s-table-header>
        <s-table-header format="numeric">Products</s-table-header>
        <s-table-header listSlot="secondary" format="numeric">
          Readiness
        </s-table-header>
        <s-table-header format="numeric">Change</s-table-header>
        <s-table-header format="numeric">Discovery score</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {points
          .map((run, i) => {
            const now = score(run.avg_readiness);
            const prev = i > 0 ? score(points[i - 1].avg_readiness) : null;
            const delta = now !== null && prev !== null ? now - prev : null;
            return (
              <s-table-row key={run.id}>
                <s-table-cell>
                  {formatDate(run.finished_at ?? run.started_at)}
                </s-table-cell>
                <s-table-cell>{String(run.products_scanned)}</s-table-cell>
                <s-table-cell>{now === null ? "-" : String(now)}</s-table-cell>
                <s-table-cell>
                  {delta === null ? (
                    "-"
                  ) : (
                    <s-badge
                      tone={
                        delta > 0
                          ? "success"
                          : delta < 0
                            ? "critical"
                            : "neutral"
                      }
                    >
                      {delta > 0 ? `+${delta}` : String(delta)}
                    </s-badge>
                  )}
                </s-table-cell>
                <s-table-cell>
                  {score(run.avg_score)?.toString() ?? "-"}
                </s-table-cell>
              </s-table-row>
            );
          })
          .reverse()}
      </s-table-body>
    </s-table>
  );
}

function RecentActivity({ overview }: { overview: CatalogOverview }) {
  const run = overview.recent_runs[0];
  const job = overview.recent_jobs[0];
  if (!run && !job) return <s-paragraph>Nothing has run yet.</s-paragraph>;
  return (
    <s-stack direction="block" gap="base">
      {run && (
        <s-stack direction="block" gap="small-300">
          <s-text type="strong">Last audit</s-text>
          <s-text color="subdued">{`${run.status}, ${run.products_scanned} products, ${formatDate(run.finished_at ?? run.started_at)}`}</s-text>
        </s-stack>
      )}
      {job && (
        <s-stack direction="block" gap="small-300">
          <s-text type="strong">Last enrichment</s-text>
          <s-text color="subdued">{`${job.status}, ${job.succeeded} of ${job.total} products done${job.failed ? `, ${job.failed} failed` : ""}`}</s-text>
        </s-stack>
      )}
      <s-link href="/app/activity">Sync and job history</s-link>
    </s-stack>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
