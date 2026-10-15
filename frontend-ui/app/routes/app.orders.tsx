import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { useFetcher, useLoaderData } from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, BackendError } from "../lib/backend.server";
import type { AgentTransaction } from "../lib/types";

const STATUS: Record<
  AgentTransaction["status"],
  { label: string; tone: "success" | "critical" | "warning" | "info" }
> = {
  order_created: { label: "Order created", tone: "success" },
  failed: { label: "Failed", tone: "critical" },
  refunded: { label: "Refunded", tone: "warning" },
  captured: { label: "Paid, needs review", tone: "critical" },
  authorized: { label: "Authorized", tone: "info" },
  pending: { label: "In progress", tone: "info" },
};

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const [transactions, payments] = await Promise.all([
    backend.transactions(session.shop),
    backend.paymentSettings(session.shop),
  ]);
  return { transactions, payments };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const value = String(
    (await request.formData()).get("stripe_account_id") ?? "",
  ).trim();
  try {
    await backend.savePaymentSettings(session.shop, value || null);
    return { ok: true, error: null };
  } catch (error) {
    return {
      ok: false,
      error:
        error instanceof BackendError && error.status === 422
          ? "Enter a Stripe account ID that starts with acct_."
          : "Could not save payment settings.",
    };
  }
};

function money(amount: string, currency: string) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
  }).format(Number(amount));
}

export default function Orders() {
  const { transactions, payments } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();

  useEffect(() => {
    if (fetcher.data?.ok) shopify.toast.show("Payment settings saved");
  }, [fetcher.data, shopify]);

  return (
    <s-page heading="Agent orders">
      {!payments.stripe_configured && (
        <s-banner tone="warning" heading="Agent payments are off">
          <s-paragraph>
            The gateway has no Stripe key, so agents can build carts but not
            pay. Set STRIPE_SECRET_KEY on the backend-engine service.
          </s-paragraph>
        </s-banner>
      )}

      <s-section heading={`Orders placed by agents (${transactions.total})`}>
        {transactions.items.length === 0 ? (
          <s-paragraph>
            No agent checkouts yet. Paid orders appear here and in Orders,
            tagged &quot;Origin: Agentic Commerce&quot;.
          </s-paragraph>
        ) : (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Order</s-table-header>
              <s-table-header>Agent</s-table-header>
              <s-table-header listSlot="secondary">Status</s-table-header>
              <s-table-header format="numeric">Amount</s-table-header>
              <s-table-header>Date</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {transactions.items.map((t) => (
                <s-table-row key={t.id}>
                  <s-table-cell>
                    {t.admin_url ? (
                      <s-link href={t.admin_url} target="_top">
                        {t.order_name ?? "Order"}
                      </s-link>
                    ) : (
                      <s-text color="subdued">
                        {t.error_detail ?? t.error_code ?? "No order"}
                      </s-text>
                    )}
                  </s-table-cell>
                  <s-table-cell>{`${t.agent_name ?? "Agent"} (${t.protocol.toUpperCase()})`}</s-table-cell>
                  <s-table-cell>
                    <s-badge tone={STATUS[t.status].tone}>
                      {STATUS[t.status].label}
                    </s-badge>
                  </s-table-cell>
                  <s-table-cell>{money(t.amount, t.currency)}</s-table-cell>
                  <s-table-cell>
                    {new Date(t.created_at).toLocaleString()}
                  </s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-section>

      <s-section slot="aside" heading="Payments">
        <fetcher.Form method="post">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              {payments.stripe_configured
                ? `Stripe is connected${payments.test_mode ? " in test mode" : ""}. Agents pay with Stripe shared payment tokens.`
                : "Stripe is not configured yet."}
            </s-paragraph>
            <s-text-field
              label="Stripe connected account ID"
              name="stripe_account_id"
              placeholder="acct_..."
              details="Leave empty to charge the gateway's own Stripe account."
              defaultValue={payments.stripe_account_id ?? ""}
            />
            {fetcher.data?.error && (
              <s-text tone="critical">{fetcher.data.error}</s-text>
            )}
            <s-button
              type="submit"
              {...(fetcher.state !== "idle" ? { loading: true } : {})}
            >
              Save
            </s-button>
          </s-stack>
        </fetcher.Form>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
