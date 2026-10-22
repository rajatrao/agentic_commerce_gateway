"""Request and response models for merchant sync and installation checks."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MerchantSyncRequest(BaseModel):
    """frontend-ui's request to record a shop's install and offline access token."""

    shop: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
    access_token: str = Field(min_length=10)
    scopes: str = ""


class MerchantOut(BaseModel):
    """A merchant's install record."""

    id: uuid.UUID
    shop_domain: str
    status: str
    installed_at: datetime


class InstallationCheckRequest(BaseModel):
    """Which storefront and product page to check."""

    store_url: str | None = Field(
        default=None, description="Defaults to the shop's myshopify.com domain."
    )
    product_url: str | None = None


class InstallationCheckOut(BaseModel):
    """The result of an installation check."""

    id: uuid.UUID
    store_url: str
    passed: bool
    jsonld_valid: bool
    has_product_schema: bool
    has_org_schema: bool
    app_block_enabled: bool
    schemas_found: list[str]
    details: dict[str, Any]
    checked_at: datetime
