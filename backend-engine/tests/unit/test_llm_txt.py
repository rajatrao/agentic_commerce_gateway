import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.services.llm_txt_renderer import render_collection, render_llm_txt, render_product
from app.services.tag_mapper import MappingRule

PRODUCTS = json.loads((Path(__file__).parent.parent / "fixtures/shopify/products.json").read_text())
SHOP = {
    "name": "Acme Outfitters",
    "description": "Trail-tested outdoor gear.",
    "url": "https://acme.myshopify.com",
    "primaryDomain": {"url": "https://acme.example", "host": "acme.example"},
    "currencyCode": "USD",
    "contactEmail": "help@acme.example",
    "shopPolicies": [
        {
            "type": "REFUND_POLICY",
            "title": "Refund policy",
            "url": "https://acme.example/policies/refund-policy",
            "body": "<p>Returns accepted within <b>30 days</b>.</p>",
        }
    ],
}


async def _aiter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


async def _render(**kwargs: Any) -> str:
    collections = kwargs.pop("collections", [])
    products = kwargs.pop("products", [])
    return "".join(
        [c async for c in render_llm_txt(SHOP, _aiter(collections), _aiter(products), **kwargs)]
    )


async def test_document_structure() -> None:
    doc = await _render(
        collections=[
            {
                "title": "Jackets",
                "handle": "jackets",
                "description": "Shells and insulated jackets.",
                "productsCount": {"count": 12},
            }
        ],
        products=[copy.deepcopy(PRODUCTS["complete"]), copy.deepcopy(PRODUCTS["sparse"])],
        faqs=[{"question": "Do you ship to Canada?", "answer": "Yes, in 3-5 days."}],
    )
    assert doc.startswith("# Acme Outfitters\n\n> Trail-tested outdoor gear.\n")
    assert "Store: https://acme.example | Currency: USD | Contact: help@acme.example" in doc
    assert (
        "- [Refund policy](https://acme.example/policies/refund-policy): "
        "Returns accepted within 30 days." in doc
    )
    assert "Q: Do you ship to Canada?\nA: Yes, in 3-5 days." in doc
    assert "- [Jackets](https://acme.example/collections/jackets): Shells and insulated" in doc
    assert "### Summit Shell Jacket" in doc
    assert "- URL: https://acme.example/products/mug" in doc  # falls back to handle URL
    assert doc.index("## Collections") < doc.index("## Products")
    assert "Each variant id below is the line item id" in doc


def test_product_block_is_dense() -> None:
    block = render_product(copy.deepcopy(PRODUCTS["complete"]), "https://acme.example")
    assert "- Brand: Acme Outfitters" in block
    assert "- Category: Apparel & Accessories > Clothing > Outerwear > Coats & Jackets" in block
    assert "- Price: 249.00 USD" in block
    assert "- Availability: In stock" in block
    assert "- Options: Size: S, M, L" in block
    assert "<p>" not in block


def test_variant_ids_are_listed_for_carts() -> None:
    p = copy.deepcopy(PRODUCTS["complete"])
    p["variants"]["nodes"][2]["availableForSale"] = False
    block = render_product(p, "https://acme.example")
    assert "- Variants (id: option, price, stock):" in block
    assert "  - 1: S, 249.00 USD, in stock, SKU SSJ-S" in block
    assert "  - 3: L, 249.00 USD, out of stock, SKU SSJ-L" in block
    single = render_product(copy.deepcopy(PRODUCTS["sparse"]), "https://x")
    assert "- Variant id: 9" in single
    assert "Variants (" not in single


def test_variant_list_is_capped() -> None:
    p = copy.deepcopy(PRODUCTS["complete"])
    base = p["variants"]["nodes"][0]
    p["variants"]["nodes"] = [
        {**base, "id": f"gid://shopify/ProductVariant/{n}", "title": f"V{n}", "sku": ""}
        for n in range(1, 31)
    ]
    block = render_product(p, "https://x")
    assert "  - 25: V25, 249.00 USD, in stock" in block
    assert "  - 26:" not in block
    assert "  - +5 more variants" in block


def test_default_title_option_hidden_and_mapping_attributes_shown() -> None:
    p = copy.deepcopy(PRODUCTS["complete"])
    rules = [MappingRule("tag", r"material:(.+)", "material")]
    assert "- Attributes: material: recycled nylon" in render_product(p, "https://x", rules)
    assert "Options" not in render_product(copy.deepcopy(PRODUCTS["sparse"]), "https://x")


def test_long_descriptions_are_clipped() -> None:
    p = copy.deepcopy(PRODUCTS["complete"])
    p["description"] = "word " * 500
    block = render_product(p, "https://x")
    assert block.rstrip().endswith("…")
    assert len(block) < 900


async def test_truncation_and_inactive_products() -> None:
    inactive = {**copy.deepcopy(PRODUCTS["partial"]), "status": "DRAFT", "title": "Hidden"}
    doc = await _render(
        products=[copy.deepcopy(PRODUCTS["complete"]), inactive, copy.deepcopy(PRODUCTS["sparse"])],
        max_products=1,
    )
    assert "Hidden" not in doc
    assert doc.count("### ") == 1
    assert "Catalog truncated at 1 products" in doc


def test_collection_line() -> None:
    line = render_collection({"title": "Sale", "handle": "sale", "description": ""}, "https://s")
    assert line == "- [Sale](https://s/collections/sale)\n"
