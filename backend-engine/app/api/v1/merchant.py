"""Merchant dashboard routes, called by frontend-ui on behalf of an authenticated merchant."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentMerchant, HttpClientDep, SessionDep, require_service_scopes
from app.models import InstallationCheck
from app.repositories.installation_checks import InstallationCheckRepository
from app.schemas.merchant import InstallationCheckOut, InstallationCheckRequest
from app.services.installation_checker import InstallationChecker, UnsafeURLError

router = APIRouter(prefix="/merchant", tags=["merchant"])


def _to_out(row: InstallationCheck) -> InstallationCheckOut:
    return InstallationCheckOut(
        id=row.id,
        store_url=row.store_url,
        passed=row.passed,
        jsonld_valid=row.jsonld_valid,
        has_product_schema=row.has_product_schema,
        has_org_schema=row.has_org_schema,
        app_block_enabled=row.app_block_enabled,
        schemas_found=row.schemas_found,
        details=row.findings,
        checked_at=row.checked_at,
    )


@router.post(
    "/installation-checks",
    response_model=InstallationCheckOut,
    dependencies=[Depends(require_service_scopes("merchant:write"))],
)
async def run_installation_check(
    body: InstallationCheckRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    http_client: HttpClientDep,
) -> InstallationCheckOut:
    checker = InstallationChecker(http_client)
    try:
        report = await checker.check(body.store_url or merchant.shop_domain, body.product_url)
    except UnsafeURLError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    row = await InstallationCheckRepository(session).save_report(merchant.id, report)
    return _to_out(row)


@router.get("/installation-checks/latest", response_model=InstallationCheckOut)
async def latest_installation_check(
    merchant: CurrentMerchant, session: SessionDep
) -> InstallationCheckOut:
    row = await InstallationCheckRepository(session).latest(merchant.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no installation check yet")
    return _to_out(row)


@router.get("/me")
async def me(merchant: CurrentMerchant) -> dict[str, str]:
    return {"shop": merchant.shop_domain, "status": merchant.status.value}
