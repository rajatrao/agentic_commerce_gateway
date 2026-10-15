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
  status: "running" | "completed" | "failed";
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
