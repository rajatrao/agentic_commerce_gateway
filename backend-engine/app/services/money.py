"""Conversion of decimal amounts to the minor-unit integers agent protocols use."""

from decimal import Decimal

# ISO 4217 currencies without minor units. Everything else in Shopify uses two decimals.
ZERO_DECIMAL = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split())


def to_minor(amount: Decimal | str, currency: str) -> int:
    """Amounts on the wire (agent protocols) are integers in the currency's minor unit."""
    value = Decimal(str(amount))
    if currency.upper() not in ZERO_DECIMAL:
        value *= 100
    return int(value.to_integral_value())
