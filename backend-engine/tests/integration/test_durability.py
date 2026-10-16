"""Multi-replica state and the durable audit queue.

"Replicas" are two app instances sharing one database, which is exactly what two processes
behind a load balancer share.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import sync_engine
from tests.integration.test_catalog import (
    GRAPHQL_PATH,
    auth,
    catalog,
    install,
    mock_shopify,
    wait_for_audit,
)


@pytest.fixture
def replicas(app_env: None) -> Iterator[tuple[TestClient, TestClient]]:
    from app.main import create_app

    with TestClient(create_app()) as a, TestClient(create_app()) as b:
        yield a, b


@pytest.fixture
def client(app_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def audit_run(run_id: Any) -> Any:
    from app.models import AuditRun

    with Session(sync_engine()) as s:
        return s.get(AuditRun, uuid.UUID(str(run_id)))


def merchant_id(shop: str) -> uuid.UUID:
    from app.models import Merchant

    with Session(sync_engine()) as s:
        merchant = s.exec(select(Merchant).where(Merchant.shop_domain == shop)).one()
        return merchant.id


# ---------------------------------------------------------------- shared state


def test_service_token_replay_is_rejected_by_another_replica(
    replicas: tuple[TestClient, TestClient],
) -> None:
    a, b = replicas
    shop = "replay-replicas.myshopify.com"
    install(a, shop)
    headers = auth(shop)
    assert a.get("/api/v1/merchant/me", headers=headers).status_code == 200
    replayed = b.get("/api/v1/merchant/me", headers=headers)
    assert replayed.status_code == 401
    assert replayed.json()["detail"] == "token replayed"


def test_llm_txt_cache_is_shared_and_invalidated_across_replicas(
    replicas: tuple[TestClient, TestClient],
) -> None:
    a, b = replicas
    shop = "llm-replicas.myshopify.com"
    install(a, shop)
    with respx.mock() as router:
        graphql = mock_shopify(router, shop, catalog())
        first = a.get(f"/api/v1/agent-gateway/llm.txt?shop={shop}")
        rendered_calls = graphql.call_count
        assert b.get(f"/api/v1/agent-gateway/llm.txt?shop={shop}").text == first.text
        assert graphql.call_count == rendered_calls  # replica B used A's cached copy

        faqs = {"faqs": [{"question": "Gift wrap?", "answer": "Yes, free."}]}
        assert b.put("/api/v1/merchant/faqs", json=faqs, headers=auth(shop)).status_code == 200
        fresh = a.get(f"/api/v1/agent-gateway/llm.txt?shop={shop}").text
    assert graphql.call_count > rendered_calls  # B's FAQ edit invalidated A's cache too
    assert "Q: Gift wrap?" in fresh


# ---------------------------------------------------------------- durable audits


def test_transient_audit_failure_is_retried(client: TestClient) -> None:
    shop = "audit-retry.myshopify.com"
    install(client, shop)
    with respx.mock() as router:
        good = mock_shopify(router, shop, catalog())
        calls = {"n": 0}
        handler = good.side_effect

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(400, text="bad gateway hiccup")
            return handler(request)  # type: ignore[operator,no-any-return,return-value]

        good.side_effect = flaky
        client.post("/api/v1/merchant/audits", headers=auth(shop))
        summary = wait_for_audit(client, shop)
    assert summary["run"]["status"] == "completed"
    assert summary["run"]["products_scanned"] == 3
    run = audit_run(summary["run"]["id"])
    assert run.attempts == 2
    assert run.last_error is None


def test_run_abandoned_by_a_dead_worker_is_reclaimed(client: TestClient) -> None:
    """A run left `running` with a stale heartbeat (its process died) is claimed again, and the
    abandoned attempt's partial results are discarded."""
    from app.models import AuditRun, ProductAudit, RunStatus
    from app.models.base import utcnow

    shop = "audit-reclaim.myshopify.com"
    install(client, shop)
    mid = merchant_id(shop)
    with Session(sync_engine()) as s:
        run = AuditRun(
            merchant_id=mid,
            status=RunStatus.RUNNING,
            attempts=1,
            heartbeat_at=utcnow() - timedelta(minutes=10),
            products_scanned=1,
        )
        s.add(run)
        s.flush()
        s.add(
            ProductAudit(
                run_id=run.id,
                merchant_id=mid,
                product_gid="gid://shopify/Product/999",
                title="Half-written by the dead worker",
                score=1,
                content_hash="x",
            )
        )
        s.commit()
        run_id = run.id

    with respx.mock() as router:
        mock_shopify(router, shop, catalog())
        summary = wait_for_audit(client, shop)
    assert summary["run"]["id"] == str(run_id)
    assert summary["run"]["status"] == "completed"
    assert summary["run"]["products_scanned"] == 3
    assert audit_run(run_id).attempts == 2
    titles = [
        p["title"]
        for p in client.get("/api/v1/merchant/audits/latest/products", headers=auth(shop)).json()[
            "items"
        ]
    ]
    assert "Half-written by the dead worker" not in titles and len(titles) == 3


def test_run_that_died_on_its_last_attempt_fails(client: TestClient) -> None:
    from app.models import AuditRun, RunStatus
    from app.models.base import utcnow

    shop = "audit-exhausted.myshopify.com"
    install(client, shop)
    with Session(sync_engine()) as s:
        run = AuditRun(
            merchant_id=merchant_id(shop),
            status=RunStatus.RUNNING,
            attempts=3,
            heartbeat_at=utcnow() - timedelta(minutes=10),
        )
        s.add(run)
        s.commit()
        run_id = run.id
    with respx.mock(assert_all_called=False) as router:
        graphql = router.post(f"https://{shop}{GRAPHQL_PATH}").respond(500)
        summary = wait_for_audit(client, shop)
    assert summary["run"]["status"] == "failed"
    assert graphql.call_count == 0
    assert audit_run(run_id).last_error == "audit worker stopped responding"


# ---------------------------------------------------------------- claim fencing


@pytest.fixture
def fenced_merchant(app_env: None) -> uuid.UUID:
    """A merchant installed through an app that is shut down again, so no background worker
    competes with the test for the runs it creates."""
    from app.main import create_app

    shop = f"audit-fence-{uuid.uuid4().hex[:8]}.myshopify.com"
    with TestClient(create_app()) as c:
        install(c, shop)
    return merchant_id(shop)


@pytest.fixture
async def database(app_env: None) -> Any:
    from app.core.config import get_settings
    from app.core.db import create_database

    db = create_database(get_settings())
    yield db
    await db.dispose()


async def test_writes_from_a_superseded_attempt_are_rejected(
    fenced_merchant: uuid.UUID, database: Any
) -> None:
    from app.repositories.audits import AuditRepository, LostClaim
    from app.services.catalog_auditor import ProductScore

    score = ProductScore("gid://shopify/Product/1", "Late write", 50, {}, [], "h")
    async with database.session() as session:
        audits = AuditRepository(session)
        run = await audits.create_run(fenced_merchant)
        claim = await audits.claim_next(stale_after=timedelta(minutes=2), max_attempts=3)
        assert claim is not None and claim.id == run.id and claim.attempt == 1

        # Another worker takes the run over (as if this one had stalled past the stale window).
        with Session(sync_engine()) as s:
            from app.models import AuditRun

            row = s.get(AuditRun, run.id)
            assert row is not None
            row.attempts = 2
            s.add(row)
            s.commit()

        assert await audits.heartbeat(claim) is False
        with pytest.raises(LostClaim):
            await audits.add_scores(claim, [score])
        with pytest.raises(LostClaim):
            await audits.finish_run(claim, None)
        rows, total = await audits.products(claim.id)
        assert total == 0 and list(rows) == []


async def test_release_requeues_without_spending_an_attempt(
    fenced_merchant: uuid.UUID, database: Any
) -> None:
    from app.models import RunStatus
    from app.repositories.audits import AuditRepository

    async with database.session() as session:
        audits = AuditRepository(session)
        run = await audits.create_run(fenced_merchant)
        claim = await audits.claim_next(stale_after=timedelta(minutes=2), max_attempts=3)
        assert claim is not None and claim.id == run.id
        await audits.release(claim)
    row = audit_run(run.id)
    assert row.status is RunStatus.QUEUED and row.attempts == 0 and row.heartbeat_at is None
