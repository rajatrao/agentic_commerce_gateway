import { useEffect, useState } from "react";
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
import type {
  CatalogJob,
  CategoryMetafieldItem,
  PublishOutcome,
  PublishPreview,
  ReviewAction,
  SuggestionStatus,
} from "../lib/types";
import { productId } from "../components/Catalog";
import { SuggestionTable } from "../components/SuggestionTable";

const PAGE_SIZE = 50;
const TABS: { status: SuggestionStatus; label: string }[] = [
  { status: "pending", label: "To review" },
  { status: "approved", label: "Approved" },
  { status: "failed", label: "Write failed" },
  { status: "published", label: "Written to Shopify" },
  { status: "rejected", label: "Rejected" },
];

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const url = new URL(request.url);
  const status = (url.searchParams.get("status") ??
    "pending") as SuggestionStatus;
  const product = url.searchParams.get("product") ?? "";
  const validation = url.searchParams.get("validation") ?? "";
  const page = Math.max(1, Number(url.searchParams.get("page") ?? 1) || 1);
  const [suggestions, overview, jobs] = await Promise.all([
    backend.suggestions(session.shop, {
      status,
      product,
      validation,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    backend.catalogOverview(session.shop),
    backend.publishJobs(session.shop, 1),
  ]);
  return {
    suggestions,
    counts: overview.suggestions,
    // The latest publish job, while it runs and for 15 minutes after it finishes.
    publishJob:
      jobs[0] &&
      (!jobs[0].finished_at ||
        Date.now() - Date.parse(jobs[0].finished_at) < 15 * 60 * 1000)
        ? jobs[0]
        : null,
    status,
    product,
    validation,
    page,
  };
};

type ActionData = {
  ok: boolean;
  message: string | null;
  error: string | null;
  preview?: PublishPreview;
  job?: CatalogJob;
};

export const action = async ({
  request,
}: ActionFunctionArgs): Promise<ActionData> => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = form.get("intent");
  const ids = form.getAll("id").map(String);
  try {
    if (intent === "preview") {
      const preview = await backend.previewPublish(
        session.shop,
        ids.length ? ids : undefined,
      );
      return { ok: true, message: null, error: null, preview };
    }
    if (intent === "publish") {
      const job = await backend.publish(session.shop, ids);
      return {
        ok: true,
        message: `Writing ${job.total} values to Shopify`,
        error: null,
        job,
      };
    }
    const actions = JSON.parse(String(form.get("actions"))) as ReviewAction[];
    const result = await backend.reviewSuggestions(session.shop, actions);
    const errors = [
      ...new Set(result.results.filter((r) => !r.ok).map((r) => r.error)),
    ];
    return {
      ok: result.failed === 0,
      message: result.succeeded
        ? `${result.succeeded} suggestions updated`
        : null,
      error: errors.length
        ? `${result.failed} could not be updated: ${errors.join("; ")}`
        : null,
    };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

const ACTIVE = ["queued", "running"];

function failures(job: CatalogJob) {
  return Object.entries(job.results)
    .filter(
      ([key, value]) =>
        !key.startsWith("taxonomy:") &&
        (value as PublishOutcome | null)?.status === "failed",
    )
    .map(([, value]) => value as PublishOutcome);
}

function categoryOutcomes(job: CatalogJob) {
  return Object.entries(job.results)
    .filter(([key]) => key.startsWith("taxonomy:"))
    .map(([, value]) => value as CategoryMetafieldItem);
}

/** Progress of the latest publish job, then its outcome. */
function PublishJobBanner({ job }: { job: CatalogJob }) {
  if (ACTIVE.includes(job.status)) {
    return (
      <s-banner tone="info" heading="Writing approved values to Shopify">
        <s-paragraph>
          {`${job.processed} of ${job.total} done. This runs in the background, so you can leave this page; finished values move to "Written to Shopify" or "Write failed".`}
        </s-paragraph>
      </s-banner>
    );
  }
  const failed = failures(job);
  const category = categoryOutcomes(job);
  const written = category.filter((c) => c.action === "written").length;
  const skippedCategory = category.filter((c) => c.action === "skipped");
  const skipped = Number(job.results.skipped ?? 0);
  if (job.status === "failed") {
    return (
      <s-banner tone="critical" heading="Writing to Shopify stopped">
        <s-paragraph>
          {`${job.processed} of ${job.total} were processed before it stopped: ${job.last_error ?? "unknown error"}. Values not yet written are still approved; write them again from the Approved tab.`}
        </s-paragraph>
      </s-banner>
    );
  }
  return (
    <s-banner
      tone={failed.length ? "warning" : "success"}
      heading={`${job.succeeded} values written to Shopify`}
    >
      <s-stack direction="block" gap="small-200">
        {failed.length > 0 && (
          <s-paragraph>
            {`${failed.length} could not be written and are listed under "Write failed": ${failed
              .slice(0, 3)
              .map((f) => `${f.product_title} (${f.attribute_key}): ${f.error}`)
              .join("; ")}${failed.length > 3 ? "; ..." : ""}`}
          </s-paragraph>
        )}
        {written > 0 && (
          <s-paragraph>
            {`${written} Shopify category metafields were also updated.`}
          </s-paragraph>
        )}
        {skippedCategory.length > 0 && (
          <s-paragraph>
            {`${skippedCategory.length} category metafields were skipped: ${skippedCategory
              .slice(0, 2)
              .map((c) => c.reason)
              .join("; ")}`}
          </s-paragraph>
        )}
        {skipped > 0 && (
          <s-paragraph>
            {`${skipped} were skipped because they were no longer approved when their turn came.`}
          </s-paragraph>
        )}
      </s-stack>
    </s-banner>
  );
}

export default function ReviewQueue() {
  const { suggestions, counts, status, product, validation, page, publishJob } =
    useLoaderData<typeof loader>();
  const revalidator = useRevalidator();
  const publishing = publishJob !== null && ACTIVE.includes(publishJob.status);
  // Poll while a publish job runs, so progress and the tabs' counts stay current.
  useEffect(() => {
    if (!publishing) return;
    const timer = setInterval(() => {
      if (revalidator.state === "idle") revalidator.revalidate();
    }, 2000);
    return () => clearInterval(timer);
  }, [publishing, revalidator]);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<PublishPreview | null>(null);
  const busyIntent =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;

  // Selection and preview follow each submit result, and reset when the filter or page
  // changes, since the rows they refer to are gone (state adjusted during render).
  const [handled, setHandled] = useState(fetcher.data);
  if (fetcher.data !== handled) {
    setHandled(fetcher.data);
    if (fetcher.data?.preview) setPreview(fetcher.data.preview);
    else if (fetcher.data?.ok || fetcher.data?.job) {
      setSelected(new Set());
      setPreview(null);
    }
  }
  const view = `${status}|${product}|${validation}|${page}`;
  const [shownView, setShownView] = useState(view);
  if (view !== shownView) {
    setShownView(view);
    setSelected(new Set());
    setPreview(null);
  }

  useEffect(() => {
    if (fetcher.data?.message) shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const go = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    next.delete("page");
    if (changes.page) next.set("page", changes.page);
    navigate(`/app/review?${next}`);
  };

  const submitReview = (actions: ReviewAction[]) =>
    fetcher.submit(
      { intent: "review", actions: JSON.stringify(actions) },
      { method: "POST" },
    );

  const submitIds = (intent: string, ids: string[]) => {
    const form = new FormData();
    form.set("intent", intent);
    ids.forEach((id) => form.append("id", id));
    fetcher.submit(form, { method: "POST" });
  };

  const bulk = (kind: "approve" | "reject") =>
    submitReview([...selected].map((id) => ({ id, action: kind })));

  const toggle = (id: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    setSelected(next);
  };

  const reviewable = suggestions.items.filter((s) =>
    ["pending", "approved", "failed"].includes(s.status),
  );
  const allSelected =
    reviewable.length > 0 && reviewable.every((s) => selected.has(s.id));
  const totalPages = Math.max(1, Math.ceil(suggestions.total / PAGE_SIZE));
  // Preview what is selected, or every approved value on this page when filtered to a
  // product, or (with neither) all approved values.
  const previewIds =
    selected.size > 0
      ? [...selected]
      : product
        ? suggestions.items.map((s) => s.id)
        : [];
  const writableIds = preview
    ? preview.items
        .filter(
          (i) =>
            (i.action === "create" || i.action === "update") &&
            !i.problems.length,
        )
        .map((i) => i.suggestion_id)
    : [];
  // Values that already match are sent too, so they are marked as written.
  const confirmIds = preview
    ? [
        ...writableIds,
        ...preview.items
          .filter((i) => i.action === "unchanged")
          .map((i) => i.suggestion_id),
      ]
    : [];

  return (
    <s-page heading="Review queue">
      {publishJob && <PublishJobBanner job={publishJob} />}
      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Not everything went through">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      <s-section>
        <s-stack direction="block" gap="base">
          <s-stack direction="inline" gap="small-200">
            {TABS.map((t) => (
              <s-button
                key={t.status}
                variant={t.status === status ? "primary" : "secondary"}
                onClick={() => go({ status: t.status })}
              >
                {`${t.label} (${counts[t.status] ?? 0})`}
              </s-button>
            ))}
          </s-stack>

          <s-stack direction="inline" gap="base" alignItems="end">
            <s-select
              label="Validation"
              value={validation}
              onChange={(e) =>
                go({ validation: e.currentTarget.value || null })
              }
            >
              <s-option value="">Any</s-option>
              <s-option value="valid">Valid</s-option>
              <s-option value="warning">Warning</s-option>
              <s-option value="invalid">Invalid</s-option>
              <s-option value="unknown">Not stated in product content</s-option>
            </s-select>
            {product && (
              <s-stack direction="inline" gap="small-200" alignItems="center">
                <s-link href={`/app/products/${productId(product)}`}>
                  Viewing one product
                </s-link>
                <s-button
                  variant="tertiary"
                  onClick={() => go({ product: null })}
                >
                  Show all products
                </s-button>
              </s-stack>
            )}
          </s-stack>

          {(status === "pending" ||
            status === "approved" ||
            status === "failed") &&
            reviewable.length > 0 && (
              <s-stack direction="inline" gap="small-200" alignItems="center">
                <s-checkbox
                  label={
                    allSelected ? "Clear selection" : "Select all on this page"
                  }
                  checked={allSelected}
                  onChange={(e) =>
                    setSelected(
                      e.currentTarget.checked
                        ? new Set(reviewable.map((s) => s.id))
                        : new Set(),
                    )
                  }
                />
                <s-button
                  disabled={!selected.size || status !== "pending"}
                  onClick={() => bulk("approve")}
                  {...(busyIntent === "review" ? { loading: true } : {})}
                >
                  {`Approve ${selected.size || ""}`.trim()}
                </s-button>
                <s-button
                  tone="critical"
                  disabled={!selected.size}
                  onClick={() => bulk("reject")}
                >
                  {`Reject ${selected.size || ""}`.trim()}
                </s-button>
                {status === "approved" && (
                  <s-button
                    variant="primary"
                    disabled={publishing}
                    onClick={() => submitIds("preview", previewIds)}
                    {...(busyIntent === "preview" ? { loading: true } : {})}
                  >
                    {selected.size
                      ? `Preview writing ${selected.size} to Shopify`
                      : product
                        ? "Preview writing this product to Shopify"
                        : "Preview writing all approved to Shopify"}
                  </s-button>
                )}
              </s-stack>
            )}

          {status === "pending" && (
            <s-text color="subdued">
              Bulk approve skips values that failed validation or are not stated
              in the product; edit those one at a time.
            </s-text>
          )}

          <SuggestionTable
            suggestions={suggestions.items}
            onReview={submitReview}
            busy={busyIntent === "review"}
            selected={selected}
            onSelect={toggle}
            showProduct
          />

          <s-stack direction="inline" gap="base" alignItems="center">
            <s-button
              disabled={page <= 1}
              onClick={() => go({ page: String(page - 1) })}
            >
              Previous
            </s-button>
            <s-text>{`Page ${page} of ${totalPages} · ${suggestions.total} suggestions`}</s-text>
            <s-button
              disabled={page >= totalPages}
              onClick={() => go({ page: String(page + 1) })}
            >
              Next
            </s-button>
          </s-stack>
        </s-stack>
      </s-section>

      {preview && (
        <s-section heading="Preview: changes to Shopify metafields">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              {`${preview.writable} will be written, ${preview.unchanged} already match, ${preview.blocked} are blocked. Writes use Shopify's compare-and-set, so a value someone edits in Shopify admin meanwhile is not overwritten. Every write is logged and can be reverted from Sync and jobs.`}
            </s-paragraph>
            <s-table variant="auto">
              <s-table-header-row>
                <s-table-header listSlot="primary">Product</s-table-header>
                <s-table-header>Metafield</s-table-header>
                <s-table-header listSlot="labeled">Now</s-table-header>
                <s-table-header listSlot="labeled">New value</s-table-header>
                <s-table-header listSlot="secondary">Result</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {preview.items.map((i) => (
                  <s-table-row key={i.suggestion_id}>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-text>{i.product_title || "Untitled product"}</s-text>
                        <s-text color="subdued">{i.attribute_key}</s-text>
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-text>{i.metafield ?? "-"}</s-text>
                        <s-text color="subdued">
                          {`${i.type ?? ""}${i.creates_definition ? " · creates definition" : ""}`}
                        </s-text>
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>{i.current_value ?? "Not set"}</s-table-cell>
                    <s-table-cell>{i.value ?? "-"}</s-table-cell>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-badge
                          tone={
                            i.action === "blocked"
                              ? "critical"
                              : i.action === "unchanged"
                                ? "neutral"
                                : "success"
                          }
                        >
                          {i.action}
                        </s-badge>
                        {i.problems.map((p) => (
                          <s-text key={p} color="subdued">
                            {p}
                          </s-text>
                        ))}
                      </s-stack>
                    </s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
            {preview.category_metafields.length > 0 && (
              <s-stack direction="block" gap="small-200">
                <s-heading>Shopify category metafields</s-heading>
                <s-paragraph>
                  These values also match your products&apos; Shopify category
                  attributes, which Shopify search and AI agents read. Values
                  are added to what the product already has; nothing is removed.
                </s-paragraph>
                <s-table variant="auto">
                  <s-table-header-row>
                    <s-table-header listSlot="primary">Product</s-table-header>
                    <s-table-header>Category attribute</s-table-header>
                    <s-table-header listSlot="labeled">Values</s-table-header>
                    <s-table-header listSlot="secondary">Result</s-table-header>
                  </s-table-header-row>
                  <s-table-body>
                    {preview.category_metafields.map((c) => (
                      <s-table-row key={`${c.product_gid}:${c.metafield}`}>
                        <s-table-cell>
                          {c.product_title || "Untitled product"}
                        </s-table-cell>
                        <s-table-cell>
                          <s-stack direction="block" gap="small-300">
                            <s-text>{c.taxonomy_attribute}</s-text>
                            <s-text color="subdued">{c.metafield}</s-text>
                          </s-stack>
                        </s-table-cell>
                        <s-table-cell>
                          {c.values.join(", ") || "-"}
                          {c.unmatched.length > 0
                            ? ` (no match for ${c.unmatched.join(", ")})`
                            : ""}
                        </s-table-cell>
                        <s-table-cell>
                          <s-stack direction="block" gap="small-300">
                            <s-badge
                              tone={
                                c.action === "add"
                                  ? "success"
                                  : c.action === "skipped"
                                    ? "warning"
                                    : "neutral"
                              }
                            >
                              {c.action === "add" ? "add" : c.action}
                            </s-badge>
                            {c.reason && (
                              <s-text color="subdued">{c.reason}</s-text>
                            )}
                          </s-stack>
                        </s-table-cell>
                      </s-table-row>
                    ))}
                  </s-table-body>
                </s-table>
              </s-stack>
            )}
            <s-stack direction="inline" gap="base">
              <s-button
                variant="primary"
                disabled={confirmIds.length === 0 || publishing}
                onClick={() => submitIds("publish", confirmIds)}
                {...(busyIntent === "publish" ? { loading: true } : {})}
              >
                {`Write ${writableIds.length} values to Shopify`}
              </s-button>
              <s-button onClick={() => setPreview(null)}>Cancel</s-button>
            </s-stack>
          </s-stack>
        </s-section>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
