import uuid

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import InstallationCheck
from app.services.installation_checker import InstallationReport


class InstallationCheckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_report(
        self, merchant_id: uuid.UUID, report: InstallationReport
    ) -> InstallationCheck:
        data = report.to_dict()
        row = InstallationCheck(
            merchant_id=merchant_id,
            store_url=report.store_url,
            passed=report.passed,
            jsonld_valid=report.jsonld_valid,
            schemas_found=report.schemas_found,
            has_product_schema=report.has_product_schema,
            has_org_schema=report.has_org_schema,
            app_block_enabled=report.app_block_enabled,
            findings={
                k: data[k]
                for k in (
                    "findings",
                    "reachable",
                    "password_protected",
                    "metadata_coverage",
                    "ai_crawlers_blocked",
                    "llms_txt_present",
                    "pages_checked",
                )
            },
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def latest(self, merchant_id: uuid.UUID) -> InstallationCheck | None:
        return (
            await self.session.exec(
                select(InstallationCheck)
                .where(InstallationCheck.merchant_id == merchant_id)
                .order_by(col(InstallationCheck.checked_at).desc())
                .limit(1)
            )
        ).first()
