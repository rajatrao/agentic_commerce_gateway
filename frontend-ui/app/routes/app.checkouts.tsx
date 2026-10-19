import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend } from "../lib/backend.server";
import type { AgentCheckout } from "../lib/types";

const STATUS: Record<
  AgentCheckout["status"],
  { label: string; tone: "success" | "critical" | "warning" | "info" }
> = {
  ready: { label: "Sent to checkout", tone: "success" },
  open: { label: "Building cart", tone: "info" },
  completed: { label: "Completed", tone: "success" },
  canceled: { label: "Canceled", tone: "warning" },
  expired: { label: "Expired", tone: "warning" },
};

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return { checkouts: await backend.agentCheckouts(session.shop) };
};

function money(amount: string, currency: string) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
  }).format(Number(amount));
}

export default function Checkouts() {
  const { checkouts } = useLoaderData<typeof loader>();

  return (
    <s-page heading="Agent checkouts">
      <s-section heading={`Carts built by agents (${checkouts.total})`}>
        {checkouts.items.length === 0 ? (
          <s-paragraph>
            No agent carts yet. When an agent builds a cart, it appears here
            with a link to your checkout.
          </s-paragraph>
        ) : (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Agent</s-table-header>
              <s-table-header listSlot="secondary">Status</s-table-header>
              <s-table-header format="numeric">Items</s-table-header>
              <s-table-header format="numeric">Total</s-table-header>
              <s-table-header>Updated</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {checkouts.items.map((c) => (
                <s-table-row key={c.id}>
                  <s-table-cell>{`${c.agent_name ?? "Agent"} (${c.protocol.toUpperCase()})`}</s-table-cell>
                  <s-table-cell>
                    <s-badge tone={STATUS[c.status].tone}>
                      {STATUS[c.status].label}
                    </s-badge>
                  </s-table-cell>
                  <s-table-cell>{c.item_count}</s-table-cell>
                  <s-table-cell>{money(c.total, c.currency)}</s-table-cell>
                  <s-table-cell>
                    {new Date(c.updated_at).toLocaleString()}
                  </s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-section>

      <s-section slot="aside" heading="How payment works">
        <s-paragraph>
          Agents build and price the cart here, then send the buyer to your own
          Shopify checkout to pay. Payments, taxes, fraud checks and payouts
          work exactly as they do for any other order.
        </s-paragraph>
        <s-paragraph>
          Paid orders appear in Orders. Each one carries a hidden
          &quot;_agentic_cart_id&quot; attribute that links it back to the agent
          cart.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
