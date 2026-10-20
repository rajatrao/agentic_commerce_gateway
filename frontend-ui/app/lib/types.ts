/** DTOs mirrored from backend-engine's OpenAPI schema (`/docs` in development). */

export type Severity = "error" | "warning" | "info";

export interface Finding {
  code: string;
  severity: Severity;
  message: string;
  page: string | null;
  fix: string | null;
}

export interface InstallationCheck {
  id: string;
  store_url: string;
  passed: boolean;
  jsonld_valid: boolean;
  has_product_schema: boolean;
  has_org_schema: boolean;
  app_block_enabled: boolean;
  schemas_found: string[];
  details: {
    findings: Finding[];
    reachable: boolean;
    password_protected: boolean;
    metadata_coverage: number;
    ai_crawlers_blocked: string[];
    llms_txt_present: boolean;
    pages_checked: string[];
  };
  checked_at: string;
}

export interface MerchantSyncResult {
  id: string;
  shop_domain: string;
  status: string;
  installed_at: string;
}

export interface AuditRun {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  started_at: string;
  finished_at: string | null;
  products_scanned: number;
  avg_score: string | null;
}

export interface AuditSummary {
  run: AuditRun | null;
  completed_run: AuditRun | null;
  ai_discovery_score: string | null;
  grade_counts: Partial<
    Record<"excellent" | "good" | "needs_work" | "poor", number>
  >;
  dimension_averages: Record<string, { points: number; max: number }>;
  top_issues: { code: string; count: number; message: string }[];
}

export interface ProductAudit {
  product_gid: string;
  title: string;
  score: number;
  breakdown: Record<string, { points: number; max: number }>;
  missing_fields: string[];
  issues: string[];
  admin_url: string;
}

export interface ProductAuditPage {
  run_id: string;
  total: number;
  items: ProductAudit[];
}

export type AgentProtocol = "ucp" | "acp" | "mcp";

export interface AgentClient {
  id: string;
  name: string;
  protocol: AgentProtocol;
  key_prefix: string;
  scopes: string[];
  created_at: string;
  revoked_at: string | null;
}

export interface AgentClientCreated extends AgentClient {
  /** Shown once; backend-engine stores only a hash. */
  api_key: string;
}

/** A cart an agent built. Buyers pay on the store's own Shopify checkout (`checkout_url`). */
export interface AgentCheckout {
  id: string;
  protocol: AgentProtocol;
  agent_name: string | null;
  status: "open" | "ready" | "completed" | "canceled" | "expired";
  item_count: number;
  total: string;
  currency: string;
  checkout_url: string | null;
  /** Set once the buyer places the order on Shopify checkout. */
  order_name: string | null;
  order_admin_url: string | null;
  ordered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentCheckoutPage {
  total: number;
  /** How many agent carts became Shopify orders. */
  ordered: number;
  items: AgentCheckout[];
}

// ------------------------------------------------------------------ catalog intelligence

export type FindingSeverity = "critical" | "high" | "medium" | "low";

export interface RunPoint {
  id: string;
  status: AuditRun["status"];
  started_at: string;
  finished_at: string | null;
  products_scanned: number;
  avg_score: string | null;
  avg_readiness: string | null;
  last_error: string | null;
}

export interface EnrichmentJob {
  id: string;
  status: AuditRun["status"];
  use_ai: boolean;
  total: number;
  succeeded: number;
  failed: number;
  created_at: string;
  finished_at: string | null;
  last_error: string | null;
  results: Record<
    string,
    {
      status: "ok" | "error";
      title?: string;
      suggestions: number;
      error: string | null;
      note?: string | null;
    }
  >;
  /** Only on the single-job endpoint. */
  product_gids?: string[] | null;
}

export interface CatalogChange {
  id: number;
  product_gid: string;
  product_title: string;
  suggestion_id?: string | null;
  attribute_key: string | null;
  action:
    | "approve"
    | "auto_approve"
    | "edit_approve"
    | "reject"
    | "reopen"
    | "publish"
    | "publish_failed"
    | "revert";
  before: unknown;
  after: unknown;
  details: Record<string, unknown>;
  created_at: string;
  reverted_at: string | null;
  revertible: boolean;
}

export interface DiscoveryTest {
  schema_key: string;
  schema_name: string;
  query: string;
  attributes: string[];
  products: number;
  supported: number;
}

/** A background publish job or discovery test run. */
export interface CatalogJob {
  id: string;
  kind: "publish" | "discovery" | "refresh";
  status: "queued" | "running" | "completed" | "failed";
  source: string;
  total: number;
  processed: number;
  succeeded: number;
  failed: number;
  created_at: string;
  finished_at: string | null;
  last_error: string | null;
  results: Record<string, unknown>;
}

/** A publish job's `results`: entries keyed by suggestion id (failed or unchanged values),
 * `taxonomy:<product>:<metafield>` for category metafields, and `skipped`. */
export interface PublishOutcome {
  status?: "failed" | "unchanged";
  product_title?: string;
  attribute_key?: string;
  error?: string | null;
}

export interface CategoryMetafieldItem {
  product_gid: string;
  product_title: string;
  attribute_key: string;
  taxonomy_attribute: string;
  metafield: string;
  values: string[];
  unmatched: string[];
  action: "add" | "unchanged" | "skipped" | "written";
  reason: string | null;
  ok?: boolean;
  error?: string | null;
}

export interface DiscoveryQueryResult {
  schema_key: string;
  schema_name: string;
  query: string;
  attributes: string[];
  method: "storefront_mcp" | "admin_search";
  error: string | null;
  results: { product_gid: string; title: string }[];
  returned: number;
  in_schema: number;
  answerable_products: number;
  answerable_found: number;
  recall: number | null;
  precision: number | null;
  missed_examples: string[];
}

export interface DiscoveryRunResults {
  method: "storefront_mcp" | "admin_search";
  fallback_reason: string | null;
  result_limit: number;
  questions: number;
  avg_recall: number | null;
  queries: DiscoveryQueryResult[];
}

export interface AutoApprovalRule {
  enabled: boolean;
  min_confidence: number;
  attributes: string[] | null;
}

export interface AutoApprovalSchema {
  schema_key: string;
  schema_name: string;
  rule: AutoApprovalRule;
  attributes: string[];
  pending: number;
  qualifying: number;
  blocked_by: Record<string, number>;
  auto_approved_total: number;
}

export interface AutoApprovalReport {
  schemas: AutoApprovalSchema[];
  recent: CatalogChange[];
  /** Current status of each suggestion in `recent`, by suggestion id. */
  statuses: Record<string, string>;
}

export interface CatalogOverview {
  has_audit: boolean;
  products_analyzed: number;
  readiness_score: string | null;
  discovery_score: string | null;
  ready_products: number;
  ready_threshold: number;
  products_missing_required: number;
  missing_attribute_instances: number;
  findings_by_severity: Partial<Record<FindingSeverity, number>>;
  top_findings: { code: string; severity: FindingSeverity; count: number }[];
  suggestions: Partial<Record<SuggestionStatus, number>>;
  failed_validations: number;
  ai_enabled: boolean;
  trend: RunPoint[];
  recent_runs: RunPoint[];
  recent_jobs: EnrichmentJob[];
  recent_changes: CatalogChange[];
  discovery_tests: DiscoveryTest[];
  discovery_run: CatalogJob | null;
  publish_job: CatalogJob | null;
}

export interface CatalogProduct {
  product_gid: string;
  title: string;
  status: string;
  product_type: string;
  vendor: string;
  category_name: string | null;
  schema_key: string | null;
  readiness_score: number | null;
  discovery_score: number | null;
  findings: Partial<Record<FindingSeverity, number>>;
  admin_url: string;
  synced_at: string;
}

export interface Page<T> {
  total: number;
  items: T[];
}

export interface CatalogFinding {
  id: number;
  product_gid: string;
  product_title: string;
  code: string;
  attribute_key: string | null;
  severity: FindingSeverity;
  method: "rule" | "ai";
  message: string;
  why_it_matters: string;
  recommended_action: string;
  evidence: string | null;
  confidence: string | null;
  created_at: string;
}

export type SuggestionStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "superseded"
  | "published"
  | "failed"
  | "reverted";

export interface Suggestion {
  id: string;
  product_gid: string;
  product_title: string;
  schema_key: string;
  attribute_key: string;
  attribute_label: string;
  data_type: string;
  unit: string | null;
  allowed_values: string[] | null;
  current_value: unknown;
  proposed_value: unknown;
  approved_value: unknown;
  display_current: string;
  display_proposed: string;
  evidence: string | null;
  evidence_source: string | null;
  confidence: string;
  method: string;
  explanation: string;
  validation_status: "valid" | "warning" | "invalid" | "unknown";
  validation_errors: string[];
  status: SuggestionStatus;
  last_error: string | null;
  created_at: string;
  reviewed_at: string | null;
  published_at: string | null;
}

export interface AttributeRow {
  key: string;
  label: string;
  type: string;
  unit: string | null;
  importance: FindingSeverity;
  required: boolean;
  current: unknown;
  current_display: string;
  source: string | null;
  errors: string[];
  metafield: string | null;
}

export interface ReadinessComponent {
  weight: number;
  value: number | null;
  applicable: boolean;
  points: number | null;
}

export interface ProductDetail {
  product: CatalogProduct;
  schema_name: string | null;
  readiness_breakdown: Record<string, ReadinessComponent>;
  attributes: AttributeRow[];
  findings: CatalogFinding[];
  suggestions: Suggestion[];
  changes: CatalogChange[];
}

export interface ReviewAction {
  id: string;
  action: "approve" | "edit" | "reject" | "reopen";
  value?: unknown;
}

export interface ReviewResult {
  results: { id: string; ok: boolean; error?: string; status?: string }[];
  succeeded: number;
  failed: number;
}

export interface PublishPreviewItem {
  suggestion_id: string;
  product_gid: string;
  product_title: string;
  attribute_key: string;
  metafield?: string;
  type?: string;
  value?: string | null;
  current_value?: string | null;
  current_type?: string | null;
  action: "create" | "update" | "unchanged" | "blocked";
  problems: string[];
  creates_definition?: boolean;
}

export interface PublishPreview {
  items: PublishPreviewItem[];
  category_metafields: CategoryMetafieldItem[];
  writable: number;
  unchanged: number;
  blocked: number;
}

export interface AttributeDefinition {
  key: string;
  label: string;
  type: string;
  unit: string | null;
  importance: FindingSeverity;
  required: boolean;
  allowed_values: string[] | null;
  aliases: string[];
  pattern: string | null;
  why: string;
  metafield: { namespace: string; key: string; type: string } | null;
  taxonomy_attribute?: string | null;
}

export interface CategorySchema {
  key: string;
  name: string;
  match_product_types: string[];
  match_keywords: string[];
  match_categories: string[];
  variant_options: string[];
  attributes: AttributeDefinition[];
  discovery_queries: { query: string; attributes: string[] }[];
  auto_approve?: AutoApprovalRule;
}

export interface SchemaEntry {
  key: string;
  customized: boolean;
  builtin: boolean;
  definition: CategorySchema;
}

export interface MetafieldMappingEntry {
  attribute_key: string;
  labels: string[];
  attribute_type: string;
  namespace: string;
  key: string;
  type: string;
  enabled: boolean;
  source: "default" | "custom";
  compatible_types: string[];
  shopify_definition: string | null;
  status: "ok" | "will_create" | "conflict" | "unknown";
}

export interface CatalogSettings {
  readiness_weights: Record<string, number>;
  default_weights: Record<string, number>;
  ready_threshold: number;
  ai_enabled: boolean;
  ai_provider: string | null;
  ai_model: string | null;
  auto_publish: boolean;
  auto_publish_note: string;
}
