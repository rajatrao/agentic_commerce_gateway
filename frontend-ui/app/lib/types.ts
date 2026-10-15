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
