"""The append-only billing audit ledger.

Every billing mutation — a subscription created, a usage event recorded, an
invoice finalized, a payment attempted, a coupon applied, a webhook received —
writes an immutable audit row. This is the billing analogue of the budget
ledger's append-only history (§11.1): a complete, replayable record of *who did
what, when, and to which entity*, so any disputed charge can be reconstructed.

This module defines the value object (:class:`AuditEntry`) and a pure in-memory
:class:`AuditLog`. The DB-backed writer (``repositories.py``) appends the same
shape. Audit writes never mutate; corrections are new entries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.billing.enums import AuditEvent


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable billing audit record."""

    event: AuditEvent
    at: datetime
    actor: str | None = None  # user id / "system" / "provider"
    customer_id: str | None = None
    subscription_id: str | None = None
    invoice_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("audit entry 'at' must be timezone-aware (UTC)")


class AuditLog:
    """An append-only in-memory audit log (pure; mirrors the DB writer)."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self,
        event: AuditEvent,
        *,
        at: datetime | None = None,
        actor: str | None = None,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        invoice_id: str | None = None,
        amount_minor: int | None = None,
        currency: str | None = None,
        **detail: object,
    ) -> AuditEntry:
        """Append an audit entry and return it."""
        entry = AuditEntry(
            event=event,
            at=at or datetime.now(tz=UTC),
            actor=actor,
            customer_id=customer_id,
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            amount_minor=amount_minor,
            currency=currency,
            detail=dict(detail),
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def for_subscription(self, subscription_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.subscription_id == subscription_id]

    def for_customer(self, customer_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.customer_id == customer_id]

    def of_type(self, event: AuditEvent) -> list[AuditEntry]:
        return [e for e in self._entries if e.event is event]

    def __len__(self) -> int:
        return len(self._entries)


def replay_amount(entries: Iterable[AuditEntry]) -> int:
    """Net minor-unit movement across a set of entries (a reconstruction aid)."""
    return sum(e.amount_minor or 0 for e in entries)


__all__ = ["AuditEntry", "AuditLog", "replay_amount"]
