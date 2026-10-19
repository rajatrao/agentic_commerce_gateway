"""An in-memory stand-in for the Shopify Admin GraphQL queries the cart and MCP tools use.

Pricing mimics `draftOrderCalculate`: two shipping rates for any address outside country "ZZ",
10% tax on items plus shipping.
"""

import copy
from decimal import Decimal
from typing import Any

V = "gid://shopify/ProductVariant/"
P = "gid://shopify/Product/"
RATES = [
    {"handle": "shopify-Express-15.00", "title": "Express", "price": "15.00"},
    {"handle": "shopify-Standard-5.00", "title": "Standard", "price": "5.00"},
]


def variant(
    vid: int,
    price: str,
    *,
    title: str = "Default Title",
    product: str = "Trail Jacket",
    stock: int | None = 10,
    policy: str = "DENY",
    tracked: bool = True,
    sellable: bool = True,
    status: str = "ACTIVE",
    ships: bool = True,
) -> dict[str, Any]:
    return {
        "id": f"{V}{vid}",
        "title": title,
        "sku": f"SKU-{vid}",
        "price": price,
        "availableForSale": sellable,
        "inventoryQuantity": stock,
        "inventoryPolicy": policy,
        "inventoryItem": {"tracked": tracked, "requiresShipping": ships},
        "image": {"url": f"https://cdn.example/{vid}.jpg"},
        "product": {
            "id": f"{P}{vid // 10}",
            "title": product,
            "status": status,
            "onlineStoreUrl": f"https://acme.example/products/{vid}",
        },
    }


def default_variants() -> dict[str, dict[str, Any]]:
    items = [
        variant(101, "120.00", title="M"),
        variant(102, "120.00", title="L", stock=1),
        variant(201, "25.00", product="Wool Socks", stock=0),
        variant(301, "40.00", product="Old Hat", status="ARCHIVED"),
        variant(401, "10.00", product="Gift Card", ships=False, tracked=False),
    ]
    return {v["id"]: v for v in items}


def _money(value: Decimal) -> dict[str, Any]:
    return {"shopMoney": {"amount": str(value), "currencyCode": "USD"}}


class FakeShopify:
    def __init__(self) -> None:
        self.variants = default_variants()
        self.calls: list[str] = []

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.handle(query, variables or {})

    def handle(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "CartVariants" in query:
            self.calls.append("variants")
            return {"nodes": [copy.deepcopy(self.variants.get(i)) for i in variables["ids"]]}
        if "CartCalculate" in query:
            self.calls.append("calculate")
            return {"draftOrderCalculate": self._calculate(variables["input"])}
        if "AgentSearch" in query:
            return {"products": {"nodes": [self._product(v) for v in self.variants.values()]}}
        if "AgentProduct" in query:
            match = [v for v in self.variants.values() if v["product"]["id"] == variables["id"]]
            return {"product": self._product(match[0]) if match else None}
        raise AssertionError(f"unexpected query: {query[:60]}")

    def _calculate(self, draft: dict[str, Any]) -> dict[str, Any]:
        rates = []
        address = draft.get("shippingAddress")
        if address and address.get("countryCode") != "ZZ":
            rates = RATES
        shipping = Decimal("0")
        title = None
        if line := draft.get("shippingLine"):
            rate = next((r for r in rates if r["handle"] == line["shippingRateHandle"]), None)
            if rate is None:
                return {
                    "calculatedDraftOrder": None,
                    "userErrors": [{"field": ["shippingLine"], "message": "Invalid rate"}],
                }
            shipping, title = Decimal(rate["price"]), rate["title"]
        lines, subtotal = [], Decimal("0")
        for li in draft["lineItems"]:
            unit = Decimal(self.variants[li["variantId"]]["price"])
            total = unit * li["quantity"]
            subtotal += total
            lines.append(
                {
                    "variant": {"id": li["variantId"]},
                    "quantity": li["quantity"],
                    "originalUnitPriceSet": _money(unit),
                    "originalTotalSet": _money(total),
                    "discountedTotalSet": _money(total),
                }
            )
        tax = ((subtotal + shipping) * Decimal("0.10")).quantize(Decimal("0.01"))
        return {
            "calculatedDraftOrder": {
                "currencyCode": "USD",
                "taxesIncluded": False,
                "subtotalPriceSet": _money(subtotal),
                "totalShippingPriceSet": _money(shipping),
                "totalTaxSet": _money(tax),
                "totalPriceSet": _money(subtotal + shipping + tax),
                "availableShippingRates": [
                    {
                        "handle": r["handle"],
                        "title": r["title"],
                        "price": {"amount": r["price"], "currencyCode": "USD"},
                    }
                    for r in rates
                ],  # fmt: skip
                "shippingLine": {"title": title} if title else None,
                "lineItems": lines,
            },
            "userErrors": [],
        }

    @staticmethod
    def _product(v: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": v["product"]["id"],
            "title": v["product"]["title"],
            "handle": "trail-jacket",
            "vendor": "Acme",
            "productType": "Jackets",
            "status": v["product"]["status"],
            "description": "A waterproof shell.",
            "onlineStoreUrl": v["product"]["onlineStoreUrl"],
            "priceRangeV2": {
                "minVariantPrice": {"amount": v["price"], "currencyCode": "USD"},
                "maxVariantPrice": {"amount": v["price"], "currencyCode": "USD"},
            },
            "featuredMedia": None,
            "variants": {
                "nodes": [
                    {
                        "id": v["id"],
                        "title": v["title"],
                        "sku": v["sku"],
                        "price": v["price"],
                        "availableForSale": v["availableForSale"],
                    }
                ]
            },
        }
