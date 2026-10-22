import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import {
  useFetcher,
  useLoaderData,
  useNavigate,
  useRevalidator,
  useSearchParams,
} from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, errorMessage } from "../lib/backend.server";
import type { CatalogJob, EnrichmentJob } from "../lib/types";
import { ChangeTable, formatDate, productId } from "../components/Catalog";

const POLL_MS = 3000;
const PAGE_SIZE = 50;

const STATUS_TONES = {
  queued: "info",
  running: "info",
  completed: "success",
  failed: "critical",
} as const;

/**
 * Loads recent audit runs, enrichment jobs, publish jobs and Shopify writes.
 */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const url = new URL(request.url);
  const writesOnly = url.searchParams.get("writes") === "1";
  const page = Math.max(1, Number(url.searchParams.get("page") ?? 1) || 1);
  const [runs, jobs, publishJobs, changes] = await Promise.all([
    backend.auditRuns(session.shop),
    backend.enrichmentJobs(session.shop),
    backend.publishJobs(session.shop, 10),
    backend.catalogChanges(session.shop, {
      writes_only: writesOnly,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
  ]);
  return { runs, jobs, publishJobs, changes, writesOnly, page };
};

/**
 * Retries an enrichment job's failed products, or reverts a published change.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  try {
    if (form.get("intent") === "retry") {
      const job = await backend.retryEnrichmentJob(
        session.shop,
        String(form.get("job_id")),
      );
      return {
        ok: true,
        message: `Retrying ${job.total} products`,
        error: null,
      };
    }
    await backend.revertChange(session.shop, Number(form.get("change_id")));
    return { ok: true, message: "Change reverted in Shopify", error: null };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

function active(status: string) {
  return status === "queued" || status === "running";
}

/**
 * Sync and jobs page: audits, enrichment jobs, writes to Shopify and the change
 * log.
 */
export default function Activity() {
  const { runs, jobs, publishJobs, changes, writesOnly, page } =
    useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const shopify = useAppBridge();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const busy = fetcher.state !== "idle" ? fetcher.formData : null;
  const working =
    runs.some((r) => active(r.status)) ||
    jobs.some((j) => active(j.status)) ||
    publishJobs.some((j) => active(j.status));

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => {
      if (revalidator.state === "idle") revalidator.revalidate();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [working, revalidator]);

  const go = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    navigate(`/app/activity?${next}`);
  };

  const totalPages = Math.max(1, Math.ceil(changes.total / PAGE_SIZE));

  return (
    <s-page heading="Sync and jobs">
      {fetcher.data?.error && (
        <s-banner tone="critical" heading="That did not work">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      <s-section heading="Catalog audits">
        <s-stack direction="block" gap="base">
          <s-paragraph>
            Each audit reads every active product from Shopify, matches it to a
            category schema and scores it. Between audits, a product is re-read
            and re-scored on its own shortly after Shopify reports a change, and
            after the app writes to it or reverts a write.
          </s-paragraph>
          {runs.length === 0 ? (
            <s-paragraph>No audits yet.</s-paragraph>
          ) : (
            <s-table variant="auto">
              <s-table-header-row>
                <s-table-header listSlot="primary">Started</s-table-header>
                <s-table-header listSlot="secondary">Status</s-table-header>
                <s-table-header format="numeric">Products</s-table-header>
                <s-table-header format="numeric">Readiness</s-table-header>
                <s-table-header format="numeric">
                  Discovery score
                </s-table-header>
                <s-table-header>Finished</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {runs.map((r) => (
                  <s-table-row key={r.id}>
                    <s-table-cell>{formatDate(r.started_at)}</s-table-cell>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-badge tone={STATUS_TONES[r.status]}>
                          {r.status}
                        </s-badge>
                        {r.last_error ? (
                          <s-text color="subdued">{r.last_error}</s-text>
                        ) : null}
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>{String(r.products_scanned)}</s-table-cell>
                    <s-table-cell>
                      {r.avg_readiness === null
                        ? "-"
                        : String(Math.round(Number(r.avg_readiness)))}
                    </s-table-cell>
                    <s-table-cell>
                      {r.avg_score === null
                        ? "-"
                        : String(Math.round(Number(r.avg_score)))}
                    </s-table-cell>
                    <s-table-cell>{formatDate(r.finished_at)}</s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
          )}
        </s-stack>
      </s-section>

      <s-section heading="Enrichment jobs">
        {jobs.length === 0 ? (
          <s-paragraph>
            No suggestions generated yet. Start from the{" "}
            <s-link href="/app/products">Catalog audit</s-link> page.
          </s-paragraph>
        ) : (
          <s-stack direction="block" gap="base">
            {jobs.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                retrying={
                  busy?.get("intent") === "retry" &&
                  busy.get("job_id") === job.id
                }
                onRetry={() =>
                  fetcher.submit(
                    { intent: "retry", job_id: job.id },
                    { method: "POST" },
                  )
                }
              />
            ))}
          </s-stack>
        )}
      </s-section>

      <s-section heading="Writes to Shopify">
        {publishJobs.length === 0 ? (
          <s-paragraph>
            Nothing written yet. Approved values are written from the{" "}
            <s-link href="/app/review?status=approved">Review queue</s-link>.
          </s-paragraph>
        ) : (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Started</s-table-header>
              <s-table-header listSlot="secondary">Status</s-table-header>
              <s-table-header format="numeric">Done</s-table-header>
              <s-table-header format="numeric">Written</s-table-header>
              <s-table-header format="numeric">Failed</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {publishJobs.map((j: CatalogJob) => (
                <s-table-row key={j.id}>
                  <s-table-cell>{formatDate(j.created_at)}</s-table-cell>
                  <s-table-cell>
                    <s-stack direction="block" gap="small-300">
                      <s-badge tone={STATUS_TONES[j.status]}>
                        {j.status}
                      </s-badge>
                      {j.last_error ? (
                        <s-text color="subdued">{j.last_error}</s-text>
                      ) : null}
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>{`${j.processed} of ${j.total}`}</s-table-cell>
                  <s-table-cell>{String(j.succeeded)}</s-table-cell>
                  <s-table-cell>{String(j.failed)}</s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-section>

      <s-section heading="Change log">
        <s-stack direction="block" gap="base">
          <s-checkbox
            label="Only show writes to Shopify"
            checked={writesOnly}
            onChange={(e) =>
              go({ writes: e.currentTarget.checked ? "1" : null, page: null })
            }
          />
          <ChangeTable
            changes={changes.items}
            busyId={
              busy?.get("intent") === "revert"
                ? Number(busy.get("change_id"))
                : null
            }
            onRevert={(id) =>
              fetcher.submit(
                { intent: "revert", change_id: String(id) },
                { method: "POST" },
              )
            }
          />
          <s-stack direction="inline" gap="base" alignItems="center">
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
      </s-section>
    </s-page>
  );
}

function JobRow({
  job,
  retrying,
  onRetry,
}: {
  job: EnrichmentJob;
  retrying: boolean;
  onRetry: () => void;
}) {
  const failures = Object.entries(job.results).filter(
    ([, r]) => r.status === "error",
  );
  return (
    <s-box padding="base" border="base" borderRadius="base">
      <s-stack direction="block" gap="small-200">
        <s-stack direction="inline" gap="base" alignItems="center">
          <s-badge tone={STATUS_TONES[job.status]}>{job.status}</s-badge>
          <s-text type="strong">{formatDate(job.created_at)}</s-text>
          <s-text color="subdued">
            {`${job.succeeded} of ${job.total} products done${job.failed ? `, ${job.failed} failed` : ""} · ${job.use_ai ? "rules and AI" : "rules only"}`}
          </s-text>
          {(job.failed > 0 || job.status === "failed") &&
            !active(job.status) && (
              <s-button
                onClick={onRetry}
                {...(retrying ? { loading: true } : {})}
              >
                Retry failed products
              </s-button>
            )}
        </s-stack>
        {job.last_error ? (
          <s-text color="subdued">{job.last_error}</s-text>
        ) : null}
        {failures.length > 0 && (
          <s-unordered-list>
            {failures.slice(0, 10).map(([gid, r]) => (
              <s-list-item key={gid}>
                <s-link href={`/app/products/${productId(gid)}`}>
                  {r.title || productId(gid)}
                </s-link>
                {`: ${r.error ?? "failed"}`}
              </s-list-item>
            ))}
          </s-unordered-list>
        )}
      </s-stack>
    </s-box>
  );
}

/** Adds the headers Shopify's embedded app boundary needs. */
export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
