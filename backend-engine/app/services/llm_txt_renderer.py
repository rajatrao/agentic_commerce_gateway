"""Renders a store as `llm.txt`: compact Markdown tuned for LLM token ingestion.

Follows the llms.txt convention (H1 title, blockquote summary, H2 sections of links) and adds a
dense product section so an agent can answer catalog questions without crawling HTML. Output is
produced incrementally so the API can stream it while paging through Shopify.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.catalog_auditor import plain_text
from app.services.tag_mapper import MappingRule, apply_rules

PRODUCT_DESCRIPTION_CHARS = 400
COLLECTION_DESCRIPTION_CHARS = 160
MAX_LISTED_VARIANTS = 25
POLICY_TITLES = {
    "REFUND_POLICY": "Refund policy",
    "SHIPPING_POLICY": "Shipping policy",
    "PRIVACY_POLICY": "Privacy policy",
    "TERMS_OF_SERVICE": "Terms of service",
    "SUBSCRIPTION_POLICY": "Subscription policy",
    "CONTACT_INFORMATION": "Contact information",
}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;: ") + "…"


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _money(amount: Any) -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)
    return f"{value:.2f}"


def _variant_id(gid: Any) -> str:
    return str(gid or "").rsplit("/", 1)[-1]


def _variant_lines(variants: list[dict[str, Any]], currency: str) -> list[str]:
    """One line per variant, keyed by the numeric id agents pass as a cart line item id."""
    listed = [v for v in variants if v.get("id")]
    if not listed:
        return []
    if len(listed) == 1 and listed[0].get("title") in (None, "", "Default Title"):
        return [f"- Variant id: {_variant_id(listed[0]['id'])}"]
    lines = ["- Variants (id: option, price, stock):"]
    for variant in listed[:MAX_LISTED_VARIANTS]:
        parts = [_one_line(variant.get("title")) or "Default"]
        if variant.get("price") is not None:
            parts.append(f"{_money(variant['price'])} {currency}".rstrip())
        parts.append("in stock" if variant.get("availableForSale") else "out of stock")
        if variant.get("sku"):
            parts.append(f"SKU {variant['sku']}")
        lines.append(f"  - {_variant_id(variant['id'])}: {', '.join(parts)}")
    if len(listed) > MAX_LISTED_VARIANTS:
        lines.append(f"  - +{len(listed) - MAX_LISTED_VARIANTS} more variants")
    return lines


def _store_url(shop: dict[str, Any]) -> str:
    return str((shop.get("primaryDomain") or {}).get("url") or shop.get("url") or "").rstrip("/")


def render_header(shop: dict[str, Any], faqs: list[dict[str, str]]) -> str:
    name = _one_line(shop.get("name")) or "Store"
    url = _store_url(shop)
    summary = _one_line(shop.get("description")) or f"Official online store of {name}."
    lines = [f"# {name}", "", f"> {summary}", ""]
    facts = [f"Store: {url}"] if url else []
    if shop.get("currencyCode"):
        facts.append(f"Currency: {shop['currencyCode']}")
    if shop.get("contactEmail"):
        facts.append(f"Contact: {shop['contactEmail']}")
    if facts:
        lines += [" | ".join(facts), ""]

    policies = [p for p in shop.get("shopPolicies") or [] if p.get("url")]
    if policies:
        lines.append("## Policies")
        for policy in policies:
            title = policy.get("title") or POLICY_TITLES.get(policy.get("type", ""), "Policy")
            summary = _clip(plain_text(None, policy.get("body")), 200)
            lines.append(f"- [{title}]({policy['url']})" + (f": {summary}" if summary else ""))
        lines.append("")

    clean_faqs = [f for f in faqs if f.get("question") and f.get("answer")]
    if clean_faqs:
        lines.append("## FAQs")
        for faq in clean_faqs:
            lines.append(f"Q: {_one_line(faq['question'])}")
            lines.append(f"A: {_one_line(faq['answer'])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_collection(collection: dict[str, Any], store_url: str) -> str:
    title = _one_line(collection.get("title"))
    url = f"{store_url}/collections/{collection.get('handle')}"
    count = (collection.get("productsCount") or {}).get("count")
    description = _clip(_one_line(collection.get("description")), COLLECTION_DESCRIPTION_CHARS)
    line = f"- [{title}]({url})"
    if description:
        line += f": {description}"
    if count is not None:
        line += f" ({count} products)"
    return line + "\n"


def render_product(
    product: dict[str, Any], store_url: str, rules: list[MappingRule] | None = None
) -> str:
    title = _one_line(product.get("title"))
    url = product.get("onlineStoreUrl") or f"{store_url}/products/{product.get('handle')}"
    lines = [f"### {title}", f"- URL: {url}"]

    mapped = apply_rules(product, rules or [])
    brand = mapped.get("brand") or _one_line(product.get("vendor"))
    if brand:
        lines.append(f"- Brand: {brand}")
    category = (product.get("category") or {}).get("fullName") or product.get("productType")
    if category:
        lines.append(f"- Category: {category}")

    price_range = product.get("priceRangeV2") or {}
    low = (price_range.get("minVariantPrice") or {}).get("amount")
    high = (price_range.get("maxVariantPrice") or {}).get("amount")
    currency = (price_range.get("minVariantPrice") or {}).get("currencyCode", "")
    if low is not None:
        price = _money(low) if low == high or high is None else f"{_money(low)}-{_money(high)}"
        lines.append(f"- Price: {price} {currency}".rstrip())

    variants = (product.get("variants") or {}).get("nodes") or []
    if variants:
        in_stock = any(v.get("availableForSale") for v in variants)
        lines.append(f"- Availability: {'In stock' if in_stock else 'Out of stock'}")
        lines += _variant_lines(variants, currency)

    options = [
        f"{o['name']}: {', '.join(o.get('values') or [])}"
        for o in product.get("options") or []
        if o.get("name") and (o.get("values") or []) not in ([], ["Default Title"])
    ]
    if options:
        lines.append(f"- Options: {'; '.join(options)}")

    attrs = {k: v for k, v in mapped.items() if k not in ("brand",)}
    if attrs:
        lines.append("- Attributes: " + "; ".join(f"{k}: {v}" for k, v in sorted(attrs.items())))

    description = _clip(
        plain_text(product.get("description"), product.get("descriptionHtml")),
        PRODUCT_DESCRIPTION_CHARS,
    )
    if description:
        lines.append(description)
    return "\n".join(lines) + "\n\n"


async def render_llm_txt(
    shop: dict[str, Any],
    collections: AsyncIterable[dict[str, Any]],
    products: AsyncIterable[dict[str, Any]],
    *,
    faqs: list[dict[str, str]] | None = None,
    rules: list[MappingRule] | None = None,
    max_products: int = 2000,
) -> AsyncIterator[str]:
    store_url = _store_url(shop)
    yield render_header(shop, faqs or [])

    first = True
    async for collection in collections:
        if first:
            yield "## Collections\n"
            first = False
        yield render_collection(collection, store_url)
    if not first:
        yield "\n"

    yield "## Products\n\n"
    yield (
        "Each variant id below is the line item id for an ACP, UCP or MCP checkout "
        "(numeric or as gid://shopify/ProductVariant/<id>).\n\n"
    )
    count = 0
    async for product in products:
        if product.get("status") not in (None, "ACTIVE"):
            continue
        if count >= max_products:
            yield f"_Catalog truncated at {max_products} products. Browse {store_url} for more._\n"
            return
        yield render_product(product, store_url, rules)
        count += 1
