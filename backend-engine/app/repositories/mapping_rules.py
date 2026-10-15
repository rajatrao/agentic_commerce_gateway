import uuid
from collections.abc import Sequence

from sqlmodel import Session, col, select

from app.models import MetaMappingRule
from app.services.tag_mapper import MappingRule


class MappingRuleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for(
        self, merchant_id: uuid.UUID, *, active_only: bool = False
    ) -> Sequence[MetaMappingRule]:
        stmt = select(MetaMappingRule).where(MetaMappingRule.merchant_id == merchant_id)
        if active_only:
            stmt = stmt.where(col(MetaMappingRule.active).is_(True))
        return self.session.exec(stmt.order_by(col(MetaMappingRule.priority))).all()

    def domain_rules(self, merchant_id: uuid.UUID) -> list[MappingRule]:
        return [
            MappingRule(
                source_type=r.source_type.value,
                source_key=r.source_key,
                match_pattern=r.match_pattern,
                target_property=r.target_property,
                transform=r.transform,
                priority=r.priority,
            )
            for r in self.list_for(merchant_id, active_only=True)
        ]

    def add(self, rule: MetaMappingRule) -> MetaMappingRule:
        self.session.add(rule)
        self.session.commit()
        self.session.refresh(rule)
        return rule

    def delete(self, merchant_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        rule = self.session.get(MetaMappingRule, rule_id)
        if rule is None or rule.merchant_id != merchant_id:
            return False
        self.session.delete(rule)
        self.session.commit()
        return True
