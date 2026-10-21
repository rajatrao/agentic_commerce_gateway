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
import type {
  MappingPreview,
  MappingRule,
  MappingRuleInput,
  MappingSourceType,
} from "../lib/types";

const SOURCES: Record<MappingSourceType, { label: string; example: string }> = {
  tag: { label: "Product tag", example: "material:(.+)" },
  metafield: { label: "Metafield", example: "(.+)" },
  product_type: { label: "Product type", example: "(.+)" },
  vendor: { label: "Vendor", example: "(.+)" },
};

// Properties the catalog audit and llm.txt feed already understand. Any other key is
// allowed and is shown to agents in llm.txt.
const PROPERTIES = [
  "brand",
  "gtin",
  "material",
  "color",
  "size",
  "pattern",
  "age_group",
  "gender",
  "care",
  "rating",
];

const CASES = {
  "": "Keep as is",
  title: "Title Case",
  upper: "UPPER",
  lower: "lower",
};

type Draft = {
  source_type: MappingSourceType;
  source_key: string;
  match_pattern: string;
  target_property: string;
  case: string;
  value: string;
  priority: string;
};

const EMPTY: Draft = {
  source_type: "tag",
  source_key: "",
  match_pattern: "",
  target_property: "brand",
  case: "",
  value: "",
  priority: "100",
};

function toInput(form: FormData): MappingRuleInput {
  const get = (k: string) => String(form.get(k) ?? "").trim();
  const transform: MappingRuleInput["transform"] = {};
  if (get("value")) transform.value = get("value");
  else if (get("case"))
    transform.case = get("case") as "title" | "upper" | "lower";
  return {
    source_type: get("source_type") as MappingSourceType,
    source_key: get("source_key"),
    match_pattern: get("match_pattern"),
    target_property: get("target_property"),
    transform,
    priority: Number.parseInt(get("priority"), 10) || 100,
    active: true,
  };
}

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  return { rules: await backend.mappingRules(session.shop) };
};

type ActionResult = {
  ok: boolean;
  message: string | null;
  preview: MappingPreview | null;
  created: boolean;
  error: string | null;
};

export const action = async ({
  request,
}: ActionFunctionArgs): Promise<ActionResult> => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = form.get("intent");
  const result = (r: Partial<ActionResult>): ActionResult => ({
    ok: true,
    message: null,
    preview: null,
    created: false,
    error: null,
    ...r,
  });
  try {
    if (intent === "delete") {
      await backend.deleteMappingRule(session.shop, String(form.get("id")));
      return result({ message: "Rule deleted" });
    }
    if (intent === "toggle") {
      const active = form.get("active") === "true";
      await backend.setMappingRuleActive(
        session.shop,
        String(form.get("id")),
        active,
      );
      return result({ message: active ? "Rule turned on" : "Rule turned off" });
    }
    const input = toInput(form);
    if (intent === "preview") {
      return result({
        preview: await backend.previewMappingRule(session.shop, input),
      });
    }
    await backend.createMappingRule(session.shop, input);
    return result({ message: "Rule saved", created: true });
  } catch (error) {
    return result({ ok: false, error: errorMessage(error) });
  }
};

export default function MappingRules() {
  const { rules } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const shopify = useAppBridge();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [preview, setPreview] = useState<MappingPreview | null>(null);
  const pending =
    fetcher.state !== "idle" ? fetcher.formData?.get("intent") : null;

  // React to a new action result while rendering, not in an effect (react.dev:
  // "Adjusting some state when a prop changes").
  const [seen, setSeen] = useState(fetcher.data);
  if (fetcher.data !== seen) {
    setSeen(fetcher.data);
    if (fetcher.data?.preview) setPreview(fetcher.data.preview);
    if (fetcher.data?.created) {
      setDraft(EMPTY);
      setPreview(null);
    }
  }

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const set = (key: keyof Draft, value: string) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setPreview(null); // a preview only describes the rule as it was
  };
  const submit = (intent: string) =>
    fetcher.submit({ intent, ...draft }, { method: "POST" });
  const ready =
    draft.match_pattern.trim() !== "" &&
    draft.target_property.trim() !== "" &&
    (draft.source_type !== "metafield" || draft.source_key.includes("."));

  return (
    <s-page heading="Meta mapping rules">
      {fetcher.data?.error && (
        <s-banner tone="critical" heading="Something went wrong">
          <s-paragraph>{fetcher.data.error}</s-paragraph>
        </s-banner>
      )}

      <s-section heading="Add a rule">
        <s-stack direction="block" gap="base">
          <s-paragraph>
            Many stores already keep facts in tags or metafields, such as a tag
            brand:acme or a custom.material metafield. A rule tells the app
            where to find a fact, so the catalog audit credits it and the
            llm.txt feed shows it to AI agents. Rules never change your products
            in Shopify.
          </s-paragraph>
          <s-select
            label="Read from"
            value={draft.source_type}
            onChange={(e) => set("source_type", e.currentTarget.value)}
          >
            {Object.entries(SOURCES).map(([value, s]) => (
              <s-option key={value} value={value}>
                {s.label}
              </s-option>
            ))}
          </s-select>
          {draft.source_type === "metafield" && (
            <s-text-field
              label="Metafield"
              details="namespace.key, for example custom.material"
              value={draft.source_key}
              onInput={(e) => set("source_key", e.currentTarget.value)}
            />
          )}
          <s-text-field
            label="Pattern"
            details="A regular expression that must match the whole value, ignoring case. The part in brackets becomes the value."
            placeholder={SOURCES[draft.source_type].example}
            value={draft.match_pattern}
            onInput={(e) => set("match_pattern", e.currentTarget.value)}
          />
          <s-text-field
            label="Property"
            details={`Lowercase key. The audit understands: ${PROPERTIES.join(", ")}`}
            value={draft.target_property}
            onInput={(e) => set("target_property", e.currentTarget.value)}
          />
          <s-stack direction="inline" gap="base">
            <s-select
              label="Letter case"
              value={draft.case}
              onChange={(e) => set("case", e.currentTarget.value)}
            >
              {Object.entries(CASES).map(([value, label]) => (
                <s-option key={value} value={value}>
                  {label}
                </s-option>
              ))}
            </s-select>
            <s-text-field
              label="Fixed value (optional)"
              details="Use this text instead of what matched"
              value={draft.value}
              onInput={(e) => set("value", e.currentTarget.value)}
            />
            <s-number-field
              label="Priority"
              details="Lower wins when two rules set the same property"
              value={draft.priority}
              onInput={(e) => set("priority", e.currentTarget.value)}
            />
          </s-stack>
          <s-stack direction="inline" gap="base">
            <s-button
              disabled={!ready}
              onClick={() => submit("preview")}
              {...(pending === "preview" ? { loading: true } : {})}
            >
              Preview on my products
            </s-button>
            <s-button
              variant="primary"
              disabled={!ready}
              onClick={() => submit("create")}
              {...(pending === "create" ? { loading: true } : {})}
            >
              Save rule
            </s-button>
          </s-stack>
          {preview && <PreviewResult preview={preview} />}
        </s-stack>
      </s-section>

      <s-section heading={`Rules (${rules.filter((r) => r.active).length} on)`}>
        {rules.length === 0 ? (
          <s-paragraph>No rules yet.</s-paragraph>
        ) : (
          <RuleTable
            rules={rules}
            onToggle={(id, active) =>
              fetcher.submit(
                { intent: "toggle", id, active: String(active) },
                { method: "POST" },
              )
            }
            onDelete={(id) =>
              fetcher.submit({ intent: "delete", id }, { method: "POST" })
            }
          />
        )}
      </s-section>

      <s-section slot="aside" heading="When rules take effect">
        <s-paragraph>
          The llm.txt feed uses a change right away. Scores on the dashboard and
          Catalog audit update on the next audit, so run one after you add or
          change rules.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

function PreviewResult({ preview }: { preview: MappingPreview }) {
  const overridden = preview.matches.filter((m) => m.overridden_by_rule).length;
  return (
    <s-box padding="base" background="subdued" borderRadius="base">
      <s-stack direction="block" gap="small-300">
        <s-text type="strong">{`Matches ${preview.matched} of the first ${preview.products_scanned} active products`}</s-text>
        {overridden > 0 && (
          <s-text color="subdued">{`${overridden} of these already get this property from a rule with a lower priority number, which wins.`}</s-text>
        )}
        {preview.matches.length > 0 && (
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Product</s-table-header>
              <s-table-header>Found</s-table-header>
              <s-table-header listSlot="secondary">Value</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {preview.matches.map((m) => (
                <s-table-row key={m.product_gid}>
                  <s-table-cell>{m.title}</s-table-cell>
                  <s-table-cell>{m.source_value}</s-table-cell>
                  <s-table-cell>
                    {m.overridden_by_rule ? `${m.value} (overridden)` : m.value}
                  </s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
        {preview.unmatched_values.length > 0 && (
          <s-text color="subdued">{`Values it did not match: ${preview.unmatched_values.join(", ")}`}</s-text>
        )}
      </s-stack>
    </s-box>
  );
}

function describe(rule: MappingRule) {
  const source =
    rule.source_type === "metafield"
      ? `Metafield ${rule.source_key}`
      : SOURCES[rule.source_type].label;
  const output = rule.transform.value
    ? `"${rule.transform.value}"`
    : rule.transform.case
      ? CASES[rule.transform.case]
      : "as matched";
  return { source, output };
}

function RuleTable({
  rules,
  onToggle,
  onDelete,
}: {
  rules: MappingRule[];
  onToggle: (id: string, active: boolean) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <s-table variant="auto">
      <s-table-header-row>
        <s-table-header listSlot="primary">Property</s-table-header>
        <s-table-header>Read from</s-table-header>
        <s-table-header>Pattern</s-table-header>
        <s-table-header>Value</s-table-header>
        <s-table-header>Priority</s-table-header>
        <s-table-header listSlot="secondary">Status</s-table-header>
        <s-table-header>Action</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {rules.map((r) => {
          const { source, output } = describe(r);
          return (
            <s-table-row key={r.id}>
              <s-table-cell>{r.target_property}</s-table-cell>
              <s-table-cell>{source}</s-table-cell>
              <s-table-cell>{r.match_pattern}</s-table-cell>
              <s-table-cell>{output}</s-table-cell>
              <s-table-cell>{String(r.priority)}</s-table-cell>
              <s-table-cell>
                {r.active ? (
                  <s-badge tone="success">On</s-badge>
                ) : (
                  <s-badge>Off</s-badge>
                )}
              </s-table-cell>
              <s-table-cell>
                <s-stack direction="inline" gap="small-200">
                  <s-button onClick={() => onToggle(r.id, !r.active)}>
                    {r.active ? "Turn off" : "Turn on"}
                  </s-button>
                  <s-button tone="critical" onClick={() => onDelete(r.id)}>
                    Delete
                  </s-button>
                </s-stack>
              </s-table-cell>
            </s-table-row>
          );
        })}
      </s-table-body>
    </s-table>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
