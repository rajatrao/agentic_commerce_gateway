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
import type { CategorySchema, SchemaEntry } from "../lib/types";
import { severityTone } from "../components/Catalog";

const NEW_SCHEMA: CategorySchema = {
  key: "my_category",
  name: "My category",
  match_product_types: ["My product type"],
  match_keywords: [],
  match_categories: [],
  variant_options: ["Size"],
  attributes: [
    {
      key: "material",
      label: "Material",
      type: "text",
      unit: null,
      importance: "high",
      required: true,
      allowed_values: null,
      aliases: ["made of", "made from"],
      pattern: null,
      why: "Shoppers and agents filter by material.",
      metafield: {
        namespace: "custom",
        key: "material",
        type: "single_line_text_field",
      },
    },
  ],
  discovery_queries: [
    { query: "a product made of recycled material", attributes: ["material"] },
  ],
};

/** FastAPI validation errors arrive as a JSON list; turn them into one readable line. */
function readableError(error: unknown) {
  const message = errorMessage(error);
  try {
    const items = JSON.parse(message) as { loc?: unknown[]; msg?: string }[];
    if (Array.isArray(items))
      return items
        .map((i) => `${(i.loc ?? []).slice(1).join(".")}: ${i.msg}`)
        .join("; ");
  } catch {
    // plain message
  }
  return message;
}

/** Loads the category schemas in effect for the store. */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return { schemas: await backend.schemas(session.shop) };
};

/** Saves an edited schema (JSON) or resets one to its default. */
export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  try {
    if (form.get("intent") === "reset") {
      await backend.resetSchema(session.shop, String(form.get("key")));
      return {
        ok: true,
        message: "Schema reset. The next audit uses it.",
        error: null,
      };
    }
    let schema: CategorySchema;
    try {
      schema = JSON.parse(String(form.get("definition"))) as CategorySchema;
    } catch {
      return { ok: false, message: null, error: "That is not valid JSON." };
    }
    await backend.saveSchema(session.shop, schema);
    return {
      ok: true,
      message: "Schema saved. The next audit uses it.",
      error: null,
    };
  } catch (error) {
    return { ok: false, message: null, error: readableError(error) };
  }
};

/**
 * Category schemas page: view and edit the attributes expected per category.
 */
export default function Schemas() {
  const { schemas } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const busy = fetcher.state !== "idle";

  // Close the editor once per successful save (state adjusted during render).
  const [handled, setHandled] = useState(fetcher.data);
  if (fetcher.data !== handled) {
    setHandled(fetcher.data);
    if (fetcher.data?.ok) setEditing(null);
  }

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const edit = (entry: SchemaEntry | null) => {
    setEditing(entry?.key ?? "__new__");
    setDraft(JSON.stringify(entry?.definition ?? NEW_SCHEMA, null, 2));
  };

  return (
    <s-page heading="Category schemas">
      <s-button
        slot="primary-action"
        variant="primary"
        onClick={() => edit(null)}
      >
        Add schema
      </s-button>

      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Schema was not saved">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      {editing && (
        <s-section
          heading={editing === "__new__" ? "New schema" : `Edit ${editing}`}
        >
          <fetcher.Form method="post">
            <s-stack direction="block" gap="base">
              <s-paragraph>
                Attribute types: text, list, number, boolean, weight, volume,
                dimension. Measurements need a unit (kg, g, lb, oz; L, ml; cm,
                mm, in). Importance is critical, high, medium or low. Discovery
                queries list the attributes an agent needs to answer them.
              </s-paragraph>
              <s-text-area
                label="Schema JSON"
                name="definition"
                rows={24}
                value={draft}
                onInput={(e) => setDraft(e.currentTarget.value)}
              />
              <s-stack direction="inline" gap="base">
                <s-button
                  type="submit"
                  variant="primary"
                  {...(busy ? { loading: true } : {})}
                >
                  Save schema
                </s-button>
                <s-button onClick={() => setEditing(null)}>Cancel</s-button>
              </s-stack>
            </s-stack>
          </fetcher.Form>
        </s-section>
      )}

      <s-section>
        <s-stack direction="block" gap="base">
          <s-paragraph>
            A schema lists the attributes AI shopping agents need for a kind of
            product. Each product is matched to one by its product type, Shopify
            category or keywords in its title; the rest use General. An
            attribute with a Shopify taxonomy attribute (marked ↗) is also
            written to the product&apos;s standard Shopify category metafield
            when the product has a category with that attribute. Auto-approval
            is set per schema on the{" "}
            <s-link href="/app/auto-approval">Auto-approval</s-link> page.
          </s-paragraph>
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Schema</s-table-header>
              <s-table-header listSlot="labeled">Matches</s-table-header>
              <s-table-header listSlot="labeled">Attributes</s-table-header>
              <s-table-header listSlot="secondary">Source</s-table-header>
              <s-table-header>Action</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {schemas.map((entry) => {
                const d = entry.definition;
                return (
                  <s-table-row key={entry.key}>
                    <s-table-cell>
                      <s-stack direction="block" gap="small-300">
                        <s-text type="strong">{d.name}</s-text>
                        <s-text color="subdued">{entry.key}</s-text>
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>
                      {[
                        ...d.match_product_types,
                        ...d.match_categories,
                        ...d.match_keywords,
                      ]
                        .slice(0, 6)
                        .join(", ") || "Everything else"}
                    </s-table-cell>
                    <s-table-cell>
                      <s-stack direction="inline" gap="small-300">
                        {d.attributes.map((a) => (
                          <s-badge
                            key={a.key}
                            tone={severityTone(a.importance)}
                          >
                            {`${a.label}${a.required ? " *" : ""}${a.taxonomy_attribute ? " ↗" : ""}`}
                          </s-badge>
                        ))}
                      </s-stack>
                    </s-table-cell>
                    <s-table-cell>
                      {entry.customized ? (
                        <s-badge tone="info">
                          {entry.builtin ? "Customized" : "Added by you"}
                        </s-badge>
                      ) : (
                        <s-badge>Built-in</s-badge>
                      )}
                      {d.auto_approve?.enabled && (
                        <s-badge tone="success">Auto-approval on</s-badge>
                      )}
                    </s-table-cell>
                    <s-table-cell>
                      <s-stack direction="inline" gap="small-300">
                        <s-button onClick={() => edit(entry)}>Edit</s-button>
                        {entry.customized && (
                          <s-button
                            tone="critical"
                            onClick={() =>
                              fetcher.submit(
                                { intent: "reset", key: entry.key },
                                { method: "POST" },
                              )
                            }
                          >
                            {entry.builtin ? "Reset" : "Delete"}
                          </s-button>
                        )}
                      </s-stack>
                    </s-table-cell>
                  </s-table-row>
                );
              })}
            </s-table-body>
          </s-table>
          <s-text color="subdued">
            * required attribute · ↗ also written to the Shopify category
            metafield
          </s-text>
        </s-stack>
      </s-section>
    </s-page>
  );
}

/** Adds the headers Shopify's embedded app boundary needs. */
export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
