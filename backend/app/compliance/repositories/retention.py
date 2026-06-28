"""Repository for per-data-class retention rules."""

from __future__ import annotations

from sqlalchemy import select

from app.compliance.db.models import RetentionRule
from app.compliance.enums import DataClass, LawfulBasis
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class RetentionRuleRepo(BaseRepository):
    """Create, upsert and query retention rules (one per data class)."""

    async def upsert(
        self,
        *,
        data_class: DataClass,
        ttl_days: int | None,
        lawful_basis: LawfulBasis,
        expire_on_consent_withdrawal: bool = False,
        description: str | None = None,
    ) -> RetentionRule:
        """Insert or update the single rule for ``data_class``."""
        rule = await self.get(data_class)
        if rule is None:
            rule = RetentionRule(
                id=new_id(),
                data_class=data_class,
                ttl_days=ttl_days,
                lawful_basis=lawful_basis,
                expire_on_consent_withdrawal=expire_on_consent_withdrawal,
                description=description,
            )
            self.session.add(rule)
        else:
            rule.ttl_days = ttl_days
            rule.lawful_basis = lawful_basis
            rule.expire_on_consent_withdrawal = expire_on_consent_withdrawal
            rule.description = description
        await self.session.flush()
        return rule

    async def get(self, data_class: DataClass) -> RetentionRule | None:
        """Fetch the rule for a data class."""
        stmt = select(RetentionRule).where(RetentionRule.data_class == data_class)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_all(self) -> list[RetentionRule]:
        """Every retention rule (the retention schedule)."""
        stmt = select(RetentionRule).order_by(RetentionRule.data_class)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["RetentionRuleRepo"]
