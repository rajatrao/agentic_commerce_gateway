import uuid

from sqlmodel import Session, col, select

from app.models import InstallationCheck
from app.services.installation_checker import InstallationReport


class InstallationCheckRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_report(self, merchant_id: uuid.UUID, report: InstallationReport) -> InstallationCheck:
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
        self.session.commit()
        self.session.refresh(row)
        return row

    def latest(self, merchant_id: uuid.UUID) -> InstallationCheck | None:
        return self.session.exec(
            select(InstallationCheck)
            .where(InstallationCheck.merchant_id == merchant_id)
            .order_by(col(InstallationCheck.checked_at).desc())
            .limit(1)
        ).first()
