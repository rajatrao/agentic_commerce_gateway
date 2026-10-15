"""Installation checker: audits a live storefront for AI-agent discoverability.

It verifies that the theme exposes:
  * valid JSON-LD (Organization / WebSite on the homepage, Product + Offer on a product page),
  * structured page metadata (OpenGraph, Twitter cards, canonical, meta description),
  * the gateway's theme app block (detected by its `data-acg-block` marker),
  * crawler access for AI agents (robots.txt) and an `llms.txt` file.

Parsing is pure (`analyze_page`, `analyze_robots`) so it can be unit tested without a network.
`InstallationChecker.check()` does the I/O with SSRF protection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

APP_BLOCK_MARKER_ATTR = "data-acg-block"

AI_CRAWLERS = (
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-User",
    "PerplexityBot",
    "Google-Extended",
)

PRODUCT_REQUIRED = ("name", "image", "description", "offers")
PRODUCT_RECOMMENDED = ("brand", "sku", "gtin", "aggregateRating")
OFFER_REQUIRED = ("price", "priceCurrency", "availability")
METADATA_TAGS = ("og:title", "og:type", "og:image", "og:url", "twitter:card")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str
    page: str | None = None
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "page": self.page,
            "fix": self.fix,
        }


@dataclass
class PageAnalysis:
    url: str
    jsonld_blocks: int = 0
    jsonld_invalid_blocks: int = 0
    types: set[str] = field(default_factory=set)
    entities: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    app_blocks: set[str] = field(default_factory=set)
    findings: list[Finding] = field(default_factory=list)

    def entities_of(self, type_name: str) -> list[dict[str, Any]]:
        return [e for e in self.entities if type_name in _types_of(e)]


@dataclass
class InstallationReport:
    store_url: str
    reachable: bool = False
    password_protected: bool = False
    jsonld_valid: bool = False
    has_product_schema: bool = False
    has_org_schema: bool = False
    app_block_enabled: bool = False
    schemas_found: list[str] = field(default_factory=list)
    metadata_coverage: float = 0.0
    ai_crawlers_blocked: list[str] = field(default_factory=list)
    llms_txt_present: bool = False
    pages_checked: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.reachable
            and not self.password_protected
            and self.jsonld_valid
            and self.has_product_schema
            and self.app_block_enabled
            and not any(f.severity is Severity.ERROR for f in self.findings)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_url": self.store_url,
            "passed": self.passed,
            "reachable": self.reachable,
            "password_protected": self.password_protected,
            "jsonld_valid": self.jsonld_valid,
            "has_product_schema": self.has_product_schema,
            "has_org_schema": self.has_org_schema,
            "app_block_enabled": self.app_block_enabled,
            "schemas_found": self.schemas_found,
            "metadata_coverage": self.metadata_coverage,
            "ai_crawlers_blocked": self.ai_crawlers_blocked,
            "llms_txt_present": self.llms_txt_present,
            "pages_checked": self.pages_checked,
            "findings": [f.to_dict() for f in self.findings],
        }


class UnsafeURLError(ValueError):
    """The URL points at a non-public address or uses a disallowed scheme."""


# --------------------------------------------------------------------------- parsing (pure)


def _types_of(entity: dict[str, Any]) -> set[str]:
    raw = entity.get("@type", [])
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1] for v in values if v}


def _flatten_jsonld(node: Any) -> list[dict[str, Any]]:
    """Return every typed entity in a JSON-LD document, including @graph and nested nodes."""
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_flatten_jsonld(item))
    elif isinstance(node, dict):
        if "@type" in node:
            out.append(node)
        for key, value in node.items():
            if key in ("@context",):
                continue
            if isinstance(value, dict | list):
                out.extend(_flatten_jsonld(value))
    return out


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict):
        return len(value) > 0
    return True


def analyze_page(url: str, html: str) -> PageAnalysis:
    soup = BeautifulSoup(html, "lxml")
    result = PageAnalysis(url=url)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        result.jsonld_blocks += 1
        marker = script.get(APP_BLOCK_MARKER_ATTR)
        if marker:
            result.app_blocks.add(str(marker))
        raw = script.string or script.get_text() or ""
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            result.jsonld_invalid_blocks += 1
            result.findings.append(
                Finding(
                    code="jsonld_parse_error",
                    severity=Severity.ERROR,
                    message=f"A JSON-LD block is not valid JSON ({exc.msg} at line {exc.lineno}).",
                    page=url,
                    fix="Fix the Liquid template that renders this block; unescaped quotes in "
                    "product descriptions are the usual cause. Use the `json` Liquid filter.",
                )
            )
            continue
        for entity in _flatten_jsonld(doc):
            result.entities.append(entity)
            result.types |= _types_of(entity)

    for el in soup.find_all(attrs={APP_BLOCK_MARKER_ATTR: True}):  # type: ignore[call-overload]
        result.app_blocks.add(str(el.get(APP_BLOCK_MARKER_ATTR)))

    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name")
        content = meta.get("content")
        if key and content:
            result.metadata[str(key).lower()] = str(content)
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        result.metadata["canonical"] = str(canonical["href"])

    return result


def validate_product_entities(page: PageAnalysis) -> list[Finding]:
    findings: list[Finding] = []
    products = page.entities_of("Product") or page.entities_of("ProductGroup")
    if not products:
        findings.append(
            Finding(
                code="product_schema_missing",
                severity=Severity.ERROR,
                message="No Product JSON-LD was found on the product page.",
                page=page.url,
                fix="Enable the 'Agentic Product Schema' app block in the theme editor.",
            )
        )
        return findings

    product = products[0]
    missing = [f for f in PRODUCT_REQUIRED if not _is_present(product.get(f))]
    if missing:
        findings.append(
            Finding(
                code="product_schema_incomplete",
                severity=Severity.ERROR,
                message=f"Product JSON-LD is missing required fields: {', '.join(missing)}.",
                page=page.url,
            )
        )

    gtin_like = any(k.startswith("gtin") or k == "mpn" for k in product)
    rec_missing = [
        f
        for f in PRODUCT_RECOMMENDED
        if not (_is_present(product.get(f)) or (f == "gtin" and gtin_like))
    ]
    if rec_missing:
        findings.append(
            Finding(
                code="product_schema_recommended_missing",
                severity=Severity.WARNING,
                message=f"Product JSON-LD lacks recommended fields: {', '.join(rec_missing)}.",
                page=page.url,
                fix="Agents rank products with identifiers (GTIN/SKU), brand and ratings higher.",
            )
        )

    offers = product.get("offers")
    offer_list = offers if isinstance(offers, list) else [offers] if offers else []
    if not offer_list and isinstance(product.get("hasVariant"), list):
        for variant in product["hasVariant"]:
            v_offers = variant.get("offers") if isinstance(variant, dict) else None
            offer_list.extend(
                v_offers if isinstance(v_offers, list) else [v_offers] if v_offers else []
            )
    for offer in offer_list:
        if not isinstance(offer, dict):
            continue
        if "AggregateOffer" in _types_of(offer):
            needed: tuple[str, ...] = ("lowPrice", "priceCurrency")
        else:
            needed = OFFER_REQUIRED
        off_missing = [f for f in needed if not _is_present(offer.get(f))]
        if off_missing:
            findings.append(
                Finding(
                    code="offer_incomplete",
                    severity=Severity.ERROR,
                    message=f"Offer is missing: {', '.join(off_missing)}.",
                    page=page.url,
                    fix="Agents cannot quote a price or confirm stock without these fields.",
                )
            )
            break
    return findings


def analyze_robots(robots_txt: str) -> list[str]:
    """Return AI crawler user-agents that are fully disallowed (`Disallow: /`)."""
    blocked: set[str] = set()
    groups: list[tuple[list[str], list[str]]] = []
    agents: list[str] = []
    rules: list[str] = []
    for line in robots_txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value)
        elif key in ("disallow", "allow"):
            rules.append(f"{key}:{value}")
    if agents:
        groups.append((agents, rules))

    def disallows_root(group_rules: list[str]) -> bool:
        return "disallow:/" in group_rules and "allow:/" not in group_rules

    wildcard_blocked = any("*" in a and disallows_root(r) for a, r in groups)
    for crawler in AI_CRAWLERS:
        specific = [r for a, r in groups if any(x.lower() == crawler.lower() for x in a)]
        if specific:
            if any(disallows_root(r) for r in specific):
                blocked.add(crawler)
        elif wildcard_blocked:
            blocked.add(crawler)
    return sorted(blocked)


# --------------------------------------------------------------------------- network


def normalise_store_url(raw: str) -> str:
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError(f"Unsupported URL: {raw!r}")
    return f"https://{parsed.hostname.lower()}"


async def assert_public_host(hostname: str) -> None:
    """Block SSRF: the host must resolve only to public, routable addresses."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Host {hostname} does not resolve") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise UnsafeURLError(f"Host {hostname} resolves to a non-public address")


class InstallationChecker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        verify_public_host: bool = True,
        max_bytes: int = 3_000_000,
    ) -> None:
        self._client = client
        self._verify_public_host = verify_public_host
        self._max_bytes = max_bytes

    async def _get(self, url: str) -> httpx.Response | None:
        try:
            resp = await self._client.get(url, follow_redirects=True)
        except httpx.HTTPError:
            return None
        if self._verify_public_host and resp.url.host:
            await assert_public_host(resp.url.host)
        if len(resp.content) > self._max_bytes:
            return None
        return resp

    async def _first_product_url(self, base: str) -> str | None:
        resp = await self._get(urljoin(base, "/products.json?limit=1"))
        if resp is not None and resp.status_code == 200:
            try:
                products = resp.json().get("products") or []
            except ValueError:
                products = []
            if products and products[0].get("handle"):
                return urljoin(base, f"/products/{products[0]['handle']}")
        return None

    async def check(self, store_url: str, product_url: str | None = None) -> InstallationReport:
        base = normalise_store_url(store_url)
        host = urlparse(base).hostname or ""
        if self._verify_public_host:
            await assert_public_host(host)

        report = InstallationReport(store_url=base)
        home = await self._get(base + "/")
        if home is None or home.status_code >= 400:
            report.findings.append(
                Finding(
                    code="store_unreachable",
                    severity=Severity.ERROR,
                    message=f"Storefront did not respond successfully"
                    f"{f' (HTTP {home.status_code})' if home is not None else ''}.",
                    page=base,
                )
            )
            return report
        report.reachable = True
        if home.url.path.rstrip("/").endswith("/password"):
            report.password_protected = True
            report.findings.append(
                Finding(
                    code="store_password_protected",
                    severity=Severity.ERROR,
                    message="The storefront is password protected, so AI agents cannot read it.",
                    page=str(home.url),
                    fix="Remove the storefront password in Online Store > Preferences.",
                )
            )
            return report

        home_page = analyze_page(str(home.url), home.text)
        pages = [home_page]

        product_url = product_url or await self._first_product_url(base)
        product_page: PageAnalysis | None = None
        if product_url:
            resp = await self._get(product_url)
            if resp is not None and resp.status_code == 200:
                product_page = analyze_page(str(resp.url), resp.text)
                pages.append(product_page)
        if product_page is None:
            report.findings.append(
                Finding(
                    code="product_page_unavailable",
                    severity=Severity.WARNING,
                    message="Could not load a product page to validate Product schema.",
                    fix="Make sure at least one product is published to the Online Store.",
                )
            )

        robots, llms = await asyncio.gather(
            self._get(urljoin(base, "/robots.txt")), self._get(urljoin(base, "/llms.txt"))
        )

        self._evaluate(report, home_page, product_page, pages, robots, llms)
        return report

    @staticmethod
    def _evaluate(
        report: InstallationReport,
        home_page: PageAnalysis,
        product_page: PageAnalysis | None,
        pages: list[PageAnalysis],
        robots: httpx.Response | None,
        llms: httpx.Response | None,
    ) -> None:
        report.pages_checked = [p.url for p in pages]
        for p in pages:
            report.findings.extend(p.findings)

        all_types = set().union(*(p.types for p in pages))
        report.schemas_found = sorted(all_types)
        total_blocks = sum(p.jsonld_blocks for p in pages)
        invalid_blocks = sum(p.jsonld_invalid_blocks for p in pages)
        report.jsonld_valid = total_blocks > 0 and invalid_blocks == 0
        if total_blocks == 0:
            report.findings.append(
                Finding(
                    code="jsonld_missing",
                    severity=Severity.ERROR,
                    message="No JSON-LD structured data was found on the storefront.",
                    fix="Enable the gateway's app blocks in the theme editor.",
                )
            )

        report.has_org_schema = bool({"Organization", "OnlineStore", "Store"} & home_page.types)
        if not report.has_org_schema:
            report.findings.append(
                Finding(
                    code="organization_schema_missing",
                    severity=Severity.WARNING,
                    message="Homepage has no Organization JSON-LD (brand name, logo, socials).",
                    page=home_page.url,
                )
            )
        if "WebSite" not in home_page.types:
            report.findings.append(
                Finding(
                    code="website_schema_missing",
                    severity=Severity.INFO,
                    message="Homepage has no WebSite JSON-LD with a SearchAction.",
                    page=home_page.url,
                )
            )

        if product_page is not None:
            product_findings = validate_product_entities(product_page)
            report.findings.extend(product_findings)
            report.has_product_schema = not any(
                f.code in ("product_schema_missing", "product_schema_incomplete")
                for f in product_findings
            )

        app_blocks = set().union(*(p.app_blocks for p in pages))
        report.app_block_enabled = bool(app_blocks)
        if not report.app_block_enabled:
            report.findings.append(
                Finding(
                    code="app_block_disabled",
                    severity=Severity.ERROR,
                    message="The Agentic Commerce Gateway theme app block is not active.",
                    fix="Open Online Store > Themes > Customize and add the "
                    "'Agentic Product Schema' block to the product template.",
                )
            )

        meta_pages = [p for p in pages if p.metadata]
        if meta_pages:
            target = product_page or home_page
            present = [t for t in METADATA_TAGS if t in target.metadata]
            extra = ["description" in target.metadata, "canonical" in target.metadata]
            report.metadata_coverage = round(
                (len(present) + sum(extra)) / (len(METADATA_TAGS) + len(extra)), 2
            )
            missing = [t for t in METADATA_TAGS if t not in target.metadata]
            if missing:
                report.findings.append(
                    Finding(
                        code="metadata_incomplete",
                        severity=Severity.WARNING,
                        message=f"Missing page metadata: {', '.join(missing)}.",
                        page=target.url,
                    )
                )

        if robots is not None and robots.status_code == 200:
            report.ai_crawlers_blocked = analyze_robots(robots.text)
            if report.ai_crawlers_blocked:
                report.findings.append(
                    Finding(
                        code="ai_crawlers_blocked",
                        severity=Severity.WARNING,
                        message="robots.txt blocks AI crawlers: "
                        + ", ".join(report.ai_crawlers_blocked)
                        + ".",
                        fix="Edit robots.txt.liquid to allow the agents you want to sell through.",
                    )
                )

        report.llms_txt_present = (
            llms is not None
            and llms.status_code == 200
            and "html" not in llms.headers.get("content-type", "")
        )
        if not report.llms_txt_present:
            report.findings.append(
                Finding(
                    code="llms_txt_missing",
                    severity=Severity.INFO,
                    message="No /llms.txt found. The gateway can serve one through an app proxy.",
                )
            )
