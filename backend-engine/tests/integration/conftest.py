"""Shared fixtures for the agent protocol and checkout tests."""

import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from tests.conftest import FRONTEND_URL
from tests.fakes import FakeShopify
from tests.integration.test_catalog import GRAPHQL_PATH


@pytest.fixture
def shopify() -> Iterator[FakeShopify]:
    fake = FakeShopify()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": fake.handle(body["query"], body["variables"])})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{FRONTEND_URL}/internal/admin-token").respond(
            json={"access_token": "shpat_fresh", "expires_at": None}
        )
        router.post(url__regex=rf"https://[^/]+{GRAPHQL_PATH}").mock(side_effect=handler)
        router.route(host="testserver").pass_through()
        yield fake
