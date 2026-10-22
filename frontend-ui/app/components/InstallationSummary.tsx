import type { Finding, InstallationCheck, Severity } from "../lib/types";

const SEVERITY_TONE: Record<Severity, "critical" | "warning" | "info"> = {
  error: "critical",
  warning: "warning",
  info: "info",
};

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <s-badge
      tone={ok ? "success" : "critical"}
    >{`${label}: ${ok ? "Yes" : "No"}`}</s-badge>
  );
}

/** Banners for each installation finding, with its fix and page. */
export function FindingsList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return (
      <s-paragraph>
        No issues found. Your storefront is ready for AI agents.
      </s-paragraph>
    );
  }
  return (
    <s-stack direction="block" gap="base">
      {findings.map((finding, index) => (
        <s-banner
          key={`${finding.code}-${index}`}
          tone={SEVERITY_TONE[finding.severity]}
          heading={finding.message}
        >
          {finding.fix && <s-paragraph>{finding.fix}</s-paragraph>}
          {finding.page && <s-text color="subdued">{finding.page}</s-text>}
        </s-banner>
      ))}
    </s-stack>
  );
}

/** Overall installation check result with a status badge per check. */
export function InstallationSummary({ check }: { check: InstallationCheck }) {
  const { details } = check;
  return (
    <s-stack direction="block" gap="base">
      <s-banner
        tone={check.passed ? "success" : "warning"}
        heading={
          check.passed
            ? "AI agents can discover and read your storefront"
            : "Your storefront needs changes before AI agents can use it"
        }
      />
      <s-stack direction="inline" gap="small-200">
        <StatusBadge ok={check.jsonld_valid} label="Valid JSON-LD" />
        <StatusBadge ok={check.has_product_schema} label="Product schema" />
        <StatusBadge ok={check.has_org_schema} label="Organization schema" />
        <StatusBadge ok={check.app_block_enabled} label="App block active" />
        <StatusBadge
          ok={details.ai_crawlers_blocked.length === 0}
          label="AI crawlers allowed"
        />
        <StatusBadge ok={details.llms_txt_present} label="llms.txt" />
      </s-stack>
      <s-paragraph>
        <s-text type="strong">Metadata coverage: </s-text>
        {Math.round(details.metadata_coverage * 100)}%{" · "}
        <s-text type="strong">Schemas found: </s-text>
        {check.schemas_found.length ? check.schemas_found.join(", ") : "none"}
      </s-paragraph>
      <s-text color="subdued">
        Checked {new Date(check.checked_at).toLocaleString()} against{" "}
        {check.store_url}
      </s-text>
    </s-stack>
  );
}
