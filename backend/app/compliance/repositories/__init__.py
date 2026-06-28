"""Async repositories over the compliance tables.

Each repo wraps an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and holds the
queries for one aggregate. Like the rest of the data layer they *flush* but never
*commit* — the unit-of-work boundary owns the transaction
(:class:`app.db.repositories.base.BaseRepository`).
"""

from __future__ import annotations

from app.compliance.repositories.consent import ConsentPolicyRepo, ConsentRecordRepo
from app.compliance.repositories.dsar import DSARRepo
from app.compliance.repositories.hold import LegalHoldRepo
from app.compliance.repositories.ledger import ComplianceLedgerRepo
from app.compliance.repositories.retention import RetentionRuleRepo

__all__ = [
    "ComplianceLedgerRepo",
    "ConsentPolicyRepo",
    "ConsentRecordRepo",
    "DSARRepo",
    "LegalHoldRepo",
    "RetentionRuleRepo",
]
