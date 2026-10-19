import { useEffect, useRef } from "react";
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
import type { AgentClient, AgentProtocol } from "../lib/types";

const PROTOCOLS: Record<AgentProtocol, { label: string; endpoint: string }> = {
  acp: {
    label: "ACP (Agentic Commerce Protocol)",
    endpoint: "/api/v1/acp/checkout_sessions",
  },
  ucp: {
    label: "UCP (Universal Commerce Protocol)",
    endpoint: "/api/v1/ucp/checkout-sessions",
  },
  mcp: { label: "MCP (Model Context Protocol)", endpoint: "/mcp" },
};

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return {
    clients: await backend.agentClients(session.shop),
    // eslint-disable-next-line no-undef
    baseUrl: (process.env.BACKEND_PUBLIC_URL ?? "").replace(/\/$/, ""),
  };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  try {
    if (form.get("intent") === "revoke") {
      await backend.revokeAgentClient(session.shop, String(form.get("id")));
      return { ok: true, message: "Key revoked", created: null, error: null };
    }
    const protocol = String(form.get("protocol")) as AgentProtocol;
    const name = String(form.get("name") ?? "").trim();
    if (!name || !(protocol in PROTOCOLS)) {
      return {
        ok: false,
        message: null,
        created: null,
        error: "Give the key a name and pick a protocol.",
      };
    }
    const created = await backend.createAgentClient(
      session.shop,
      name,
      protocol,
    );
    return { ok: true, message: "Key created", created, error: null };
  } catch (error) {
    return {
      ok: false,
      message: null,
      created: null,
      error:
        error instanceof BackendError
          ? error.detail
          : "The gateway engine is unreachable.",
    };
  }
};

export default function Agents() {
  const { clients, baseUrl } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const form = useRef<HTMLFormElement>(null);
  const busy = fetcher.state !== "idle";
  const created = fetcher.data?.created;

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message) {
      shopify.toast.show(fetcher.data.message);
      if (fetcher.data.created) form.current?.reset();
    }
  }, [fetcher.data, shopify]);

  const active = clients.filter((c) => !c.revoked_at);

  return (
    <s-page heading="Agent access">
      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Could not update agent keys">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}
      {created && (
        <s-banner tone="success" heading={`API key for ${created.name}`}>
          <s-stack direction="block" gap="small-200">
            <s-paragraph>
              Copy this key now. It is shown only once; give it to the agent as
              a Bearer token.
            </s-paragraph>
            <s-text type="strong">{created.api_key}</s-text>
            <s-text color="subdued">{`Endpoint: ${baseUrl}${PROTOCOLS[created.protocol].endpoint}`}</s-text>
          </s-stack>
        </s-banner>
      )}

      <s-section heading="Create a key">
        <fetcher.Form method="post" ref={form}>
          <s-stack direction="block" gap="base">
            <s-text-field
              label="Name"
              name="name"
              placeholder="e.g. ChatGPT checkout"
              required
            />
            <s-select label="Protocol" name="protocol">
              {Object.entries(PROTOCOLS).map(([value, p]) => (
                <s-option key={value} value={value}>
                  {p.label}
                </s-option>
              ))}
            </s-select>
            <s-button
              type="submit"
              variant="primary"
              {...(busy ? { loading: true } : {})}
            >
              Create key
            </s-button>
          </s-stack>
        </fetcher.Form>
      </s-section>

      <s-section heading={`Keys (${active.length} active)`}>
        {clients.length === 0 ? (
          <s-paragraph>
            No agent keys yet. Create one for each AI agent or platform that
            should be able to build carts in your store.
          </s-paragraph>
        ) : (
          <AgentClientTable
            clients={clients}
            onRevoke={(id) =>
              fetcher.submit({ intent: "revoke", id }, { method: "POST" })
            }
          />
        )}
      </s-section>

      <s-section slot="aside" heading="How agents connect">
        <s-paragraph>
          Agents send the key as a Bearer token. UCP and ACP calls also need an
          Idempotency-Key header. Carts are priced with your own shipping rates
          and taxes, and stock is checked live. When a cart is ready, the agent
          sends the buyer to your own Shopify checkout to pay.
        </s-paragraph>
        {baseUrl && (
          <s-unordered-list>
            {Object.values(PROTOCOLS).map((p) => (
              <s-list-item key={p.endpoint}>
                <s-text>{`${baseUrl}${p.endpoint}`}</s-text>
              </s-list-item>
            ))}
          </s-unordered-list>
        )}
      </s-section>
    </s-page>
  );
}

function AgentClientTable({
  clients,
  onRevoke,
}: {
  clients: AgentClient[];
  onRevoke: (id: string) => void;
}) {
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Name</s-table-header>
        <s-table-header>Protocol</s-table-header>
        <s-table-header>Key</s-table-header>
        <s-table-header listSlot="secondary">Status</s-table-header>
        <s-table-header>Action</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {clients.map((c) => (
          <s-table-row key={c.id}>
            <s-table-cell>{c.name}</s-table-cell>
            <s-table-cell>{c.protocol.toUpperCase()}</s-table-cell>
            <s-table-cell>{`acg_live_${c.key_prefix}_…`}</s-table-cell>
            <s-table-cell>
              {c.revoked_at ? (
                <s-badge tone="critical">Revoked</s-badge>
              ) : (
                <s-badge tone="success">Active</s-badge>
              )}
            </s-table-cell>
            <s-table-cell>
              {!c.revoked_at && (
                <s-button tone="critical" onClick={() => onRevoke(c.id)}>
                  Revoke
                </s-button>
              )}
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
