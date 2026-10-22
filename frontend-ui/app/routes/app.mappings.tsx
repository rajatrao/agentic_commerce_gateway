import { useEffect, useState } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import {
  useFetcher,
  useLoaderData,
  useNavigation,
  useSearchParams,
} from "react-router";
import { useAppBridge } from "@shopify/app-bridge-react";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { backend, errorMessage } from "../lib/backend.server";
import type { MetafieldMappingEntry } from "../lib/types";

const STATUS = {
  ok: { tone: "success", label: "Matches Shopify" },
  will_create: { tone: "info", label: "Definition will be created" },
  conflict: { tone: "critical", label: "Type conflict" },
  unknown: { tone: "neutral", label: "Not checked" },
} as const;

/**
 * Loads the attribute-to-metafield mappings, checked against Shopify when
 * `?check=1`.
 */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const check = new URL(request.url).searchParams.get("check") === "1";
  try {
    return {
      mappings: await backend.mappings(session.shop, check),
      checked: check,
      checkError: null,
    };
  } catch (error) {
    if (!check) throw error;
    return {
      mappings: await backend.mappings(session.shop, false),
      checked: false,
      checkError: errorMessage(error),
    };
  }
};

/** Saves the metafield an attribute is written to. */
export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  try {
    await backend.saveMapping(session.shop, String(form.get("attribute")), {
      namespace: String(form.get("namespace")).trim(),
      key: String(form.get("key")).trim(),
      type: String(form.get("type")),
      enabled: form.get("enabled") === "true",
    });
    return { ok: true, message: "Mapping saved", error: null };
  } catch (error) {
    return { ok: false, message: null, error: errorMessage(error) };
  }
};

/**
 * Metafield mapping page: where each schema attribute is written in Shopify.
 */
export default function Mappings() {
  const { mappings, checked, checkError } = useLoaderData<typeof loader>();
  const fetcher = useFetcher<typeof action>();
  const navigation = useNavigation();
  const [, setParams] = useSearchParams();
  const shopify = useAppBridge();

  useEffect(() => {
    if (fetcher.data?.ok && fetcher.data.message)
      shopify.toast.show(fetcher.data.message);
  }, [fetcher.data, shopify]);

  const conflicts = mappings.filter((m) => m.status === "conflict").length;

  return (
    <s-page heading="Metafield mapping">
      <s-button
        slot="primary-action"
        onClick={() => setParams({ check: "1" })}
        {...(navigation.state === "loading" ? { loading: true } : {})}
      >
        Check against Shopify
      </s-button>

      {(fetcher.data?.error || checkError) && (
        <s-banner tone="critical" heading="Something went wrong">
          <s-paragraph>{fetcher.data?.error ?? checkError}</s-paragraph>
        </s-banner>
      )}
      {checked && conflicts > 0 && (
        <s-banner
          tone="warning"
          heading={`${conflicts} mappings conflict with Shopify`}
        >
          <s-paragraph>
            A metafield with that namespace and key already exists in Shopify
            with a different type. Writes to it are blocked until you change the
            type to match or pick another key.
          </s-paragraph>
        </s-banner>
      )}

      <s-section>
        <s-stack direction="block" gap="base">
          <s-paragraph>
            Approved attribute values are written to these product metafields as
            typed values, so agents, search and your theme can read them. When a
            definition does not exist yet, it is created on the first write.
            Turn a mapping off to keep that attribute in the app only.
          </s-paragraph>
          <s-table variant="auto">
            <s-table-header-row>
              <s-table-header listSlot="primary">Attribute</s-table-header>
              <s-table-header>Namespace</s-table-header>
              <s-table-header>Key</s-table-header>
              <s-table-header>Metafield type</s-table-header>
              <s-table-header>Write</s-table-header>
              <s-table-header listSlot="secondary">Shopify</s-table-header>
              <s-table-header>Action</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {mappings.map((m) => (
                <MappingRow
                  key={m.attribute_key}
                  mapping={m}
                  saving={
                    fetcher.state !== "idle" &&
                    fetcher.formData?.get("attribute") === m.attribute_key
                  }
                  onSave={(values) =>
                    fetcher.submit(
                      { attribute: m.attribute_key, ...values },
                      { method: "POST" },
                    )
                  }
                />
              ))}
            </s-table-body>
          </s-table>
        </s-stack>
      </s-section>
    </s-page>
  );
}

function MappingRow({
  mapping,
  saving,
  onSave,
}: {
  mapping: MetafieldMappingEntry;
  saving: boolean;
  onSave: (values: Record<string, string>) => void;
}) {
  const [namespace, setNamespace] = useState(mapping.namespace);
  const [key, setKey] = useState(mapping.key);
  const [type, setType] = useState(mapping.type);
  const [enabled, setEnabled] = useState(mapping.enabled);
  const dirty =
    namespace !== mapping.namespace ||
    key !== mapping.key ||
    type !== mapping.type ||
    enabled !== mapping.enabled;
  const status = STATUS[mapping.status];

  return (
    <s-table-row>
      <s-table-cell>
        <s-stack direction="block" gap="small-300">
          <s-text type="strong">
            {mapping.labels[0] ?? mapping.attribute_key}
          </s-text>
          <s-text color="subdued">{`${mapping.attribute_key} · ${mapping.attribute_type}${mapping.source === "custom" ? " · customized" : ""}`}</s-text>
        </s-stack>
      </s-table-cell>
      <s-table-cell>
        <s-text-field
          label="Namespace"
          labelAccessibilityVisibility="exclusive"
          value={namespace}
          onInput={(e) => setNamespace(e.currentTarget.value)}
        />
      </s-table-cell>
      <s-table-cell>
        <s-text-field
          label="Key"
          labelAccessibilityVisibility="exclusive"
          value={key}
          onInput={(e) => setKey(e.currentTarget.value)}
        />
      </s-table-cell>
      <s-table-cell>
        <s-select
          label="Type"
          labelAccessibilityVisibility="exclusive"
          value={type}
          onChange={(e) => setType(e.currentTarget.value)}
        >
          {mapping.compatible_types.map((t) => (
            <s-option key={t} value={t}>
              {t}
            </s-option>
          ))}
        </s-select>
      </s-table-cell>
      <s-table-cell>
        <s-checkbox
          accessibilityLabel={`Write ${mapping.attribute_key} to Shopify`}
          checked={enabled}
          onChange={(e) => setEnabled(e.currentTarget.checked)}
        />
      </s-table-cell>
      <s-table-cell>
        <s-stack direction="block" gap="small-300">
          <s-badge tone={status.tone}>{status.label}</s-badge>
          {mapping.status === "conflict" && mapping.shopify_definition ? (
            <s-text color="subdued">{`Defined as ${mapping.shopify_definition}`}</s-text>
          ) : null}
        </s-stack>
      </s-table-cell>
      <s-table-cell>
        <s-button
          disabled={!dirty || !namespace.trim() || !key.trim()}
          onClick={() =>
            onSave({ namespace, key, type, enabled: String(enabled) })
          }
          {...(saving ? { loading: true } : {})}
        >
          Save
        </s-button>
      </s-table-cell>
    </s-table-row>
  );
}

/** Adds the headers Shopify's embedded app boundary needs. */
export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
