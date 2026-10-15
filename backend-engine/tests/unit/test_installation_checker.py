from pathlib import Path

import httpx
import pytest
import respx

from app.services.installation_checker import (
    InstallationChecker,
    UnsafeURLError,
    analyze_page,
    analyze_robots,
    normalise_store_url,
    validate_product_entities,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "storefront"
BASE = "https://acme.example"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def codes(findings) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.code for f in findings}


class TestAnalyzePage:
    def test_graph_entities_and_metadata(self) -> None:
        page = analyze_page(BASE, fixture("home.html"))
        assert page.jsonld_blocks == 1
        assert page.jsonld_invalid_blocks == 0
        assert {"Organization", "WebSite", "SearchAction"} <= page.types
        assert page.metadata["og:type"] == "website"
        assert page.metadata["canonical"] == "https://acme.example/"
        assert page.app_blocks == set()

    def test_app_block_marker_detected(self) -> None:
        page = analyze_page(f"{BASE}/products/x", fixture("product_good.html"))
        assert page.app_blocks == {"product-jsonld"}
        assert validate_product_entities(page) == []

    def test_broken_jsonld_and_incomplete_offer(self) -> None:
        page = analyze_page(f"{BASE}/products/x", fixture("product_broken.html"))
        assert page.jsonld_invalid_blocks == 1
        assert "jsonld_parse_error" in codes(page.findings)
        found = codes(validate_product_entities(page))
        assert {"product_schema_incomplete", "offer_incomplete"} <= found

    def test_missing_product_schema(self) -> None:
        page = analyze_page(BASE, fixture("home.html"))
        assert codes(validate_product_entities(page)) == {"product_schema_missing"}


class TestRobots:
    def test_specific_and_grouped_agents_blocked(self) -> None:
        assert analyze_robots(fixture("robots_blocking.txt")) == [
            "ClaudeBot",
            "GPTBot",
            "PerplexityBot",
        ]

    def test_wildcard_block_applies_to_unlisted_crawlers(self) -> None:
        txt = "User-agent: *\nDisallow: /\n\nUser-agent: GPTBot\nAllow: /\n"
        blocked = analyze_robots(txt)
        assert "GPTBot" not in blocked
        assert "ClaudeBot" in blocked

    def test_shopify_default_does_not_block(self) -> None:
        assert analyze_robots("User-agent: *\nDisallow: /admin\nDisallow: /checkout\n") == []


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("acme.myshopify.com", "https://acme.myshopify.com"),
            ("http://Acme.Example/some/path?q=1", "https://acme.example"),
        ],
    )
    def test_normalise(self, raw: str, expected: str) -> None:
        assert normalise_store_url(raw) == expected

    def test_rejects_other_schemes(self) -> None:
        with pytest.raises(UnsafeURLError):
            normalise_store_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_ssrf_guard_rejects_private_addresses() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(UnsafeURLError):
            await InstallationChecker(client).check("http://localhost")


def _mock_store(router: respx.Router, product_html: str, robots: str = "User-agent: *\n") -> None:
    router.get(f"{BASE}/").respond(200, html=fixture("home.html"))
    router.get(f"{BASE}/products.json", params={"limit": "1"}).respond(
        200, json={"products": [{"handle": "summit-shell"}]}
    )
    router.get(f"{BASE}/products/summit-shell").respond(200, html=product_html)
    router.get(f"{BASE}/robots.txt").respond(200, text=robots)
    router.get(f"{BASE}/llms.txt").respond(404, html="<html>not found</html>")


@pytest.mark.asyncio
async def test_full_check_passes_for_well_configured_store() -> None:
    with respx.mock(assert_all_called=True) as router:
        _mock_store(router, fixture("product_good.html"))
        async with httpx.AsyncClient() as client:
            report = await InstallationChecker(client, verify_public_host=False).check(BASE)

    assert report.passed
    assert report.jsonld_valid and report.has_product_schema and report.has_org_schema
    assert report.app_block_enabled
    assert report.metadata_coverage == 1.0
    assert "Product" in report.schemas_found
    assert codes(report.findings) == {"llms_txt_missing"}


@pytest.mark.asyncio
async def test_full_check_reports_problems() -> None:
    with respx.mock() as router:
        _mock_store(router, fixture("product_broken.html"), robots=fixture("robots_blocking.txt"))
        async with httpx.AsyncClient() as client:
            report = await InstallationChecker(client, verify_public_host=False).check(BASE)

    assert not report.passed
    assert not report.jsonld_valid
    assert not report.app_block_enabled
    assert report.ai_crawlers_blocked == ["ClaudeBot", "GPTBot", "PerplexityBot"]
    assert {
        "jsonld_parse_error",
        "product_schema_incomplete",
        "offer_incomplete",
        "app_block_disabled",
        "ai_crawlers_blocked",
        "metadata_incomplete",
    } <= codes(report.findings)


@pytest.mark.asyncio
async def test_password_protected_store() -> None:
    with respx.mock() as router:
        router.get(f"{BASE}/").respond(302, headers={"Location": f"{BASE}/password"})
        router.get(f"{BASE}/password").respond(200, html="<html>Enter password</html>")
        async with httpx.AsyncClient() as client:
            report = await InstallationChecker(client, verify_public_host=False).check(BASE)
    assert report.password_protected
    assert not report.passed
    assert codes(report.findings) == {"store_password_protected"}
