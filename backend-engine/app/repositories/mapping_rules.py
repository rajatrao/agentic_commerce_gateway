import uuid
from collections.abc import Sequence

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import MetaMappingRule
from app.services.tag_mapper import MappingRule


class MappingRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for(
        self, merchant_id: uuid.UUID, *, active_only: bool = False
    ) -> Sequence[MetaMappingRule]:
        stmt = select(MetaMappingRule).where(MetaMappingRule.merchant_id == merchant_id)
        if active_only:
            stmt = stmt.where(col(MetaMappingRule.active).is_(True))
        return (await self.session.exec(stmt.order_by(col(MetaMappingRule.priority)))).all()

    async def domain_rules(self, merchant_id: uuid.UUID) -> list[MappingRule]:
        return [
            MappingRule(
                source_type=r.source_type.value,
                source_key=r.source_key,
                match_pattern=r.match_pattern,
                target_property=r.target_property,
                transform=r.transform,
                priority=r.priority,
            )
            for r in await self.list_for(merchant_id, active_only=True)
        ]

    async def add(self, rule: MetaMappingRule) -> MetaMappingRule:
        self.session.add(rule)
        await self.session.commit()
        await self.session.refresh(rule)
        return rule

    async def get(self, merchant_id: uuid.UUID, rule_id: uuid.UUID) -> MetaMappingRule | None:
        rule = await self.session.get(MetaMappingRule, rule_id)
        return rule if rule is not None and rule.merchant_id == merchant_id else None

    async def delete(self, merchant_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        rule = await self.get(merchant_id, rule_id)
        if rule is None:
            return False
        await self.session.delete(rule)
        await self.session.commit()
        return True
