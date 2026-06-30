"""The typed :class:`AuditEvent` — the input contract for the audit log.

An :class:`AuditEvent` is the *logical* description a call site hands to the
:class:`~app.audit.service.AuditService`. The service assigns the sequence
number, redacts, hashes, and chains it; the event itself carries only the facts:

* **who** — ``actor_kind`` + ``actor_id`` (e.g. agent ``continuity``, user
  ``usr_42``, system ``render-worker``);
* **what** — ``category`` + ``action`` + ``severity``;
* **on what** — ``target_type`` + ``target_id`` (the clip / canon-fact / shot /
  session the action concerns — the spine of the provenance trail);
* **before / after** — optional state snapshots (a canon mutation's old/new node,
  a flag's old/new value) so the diff is reconstructable;
* **why** — free-text ``reason`` (a Director's note, an arbitration rationale);
* **correlation / trace** — ``correlation_id`` ties every event for one render /
  session together; ``trace_id`` ties it to a distributed trace span;
* **when** — ``occurred_at`` (the event's own logical UTC timestamp).

Pydantic v2 model with validation: enums are coerced from their string values,
``occurred_at`` is normalised to UTC, and a coherence check rejects an
incoherent (category, action) pair early (``OTHER`` is the escape hatch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
    category_for_action,
    default_severity,
    is_coherent,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEvent(BaseModel):
    """A single consequential action, ready to be appended to the audit log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: AuditCategory
    action: AuditAction
    severity: AuditSeverity | None = None
    actor_kind: AuditActorKind
    actor_id: str = Field(min_length=1, max_length=128)

    target_type: str | None = Field(default=None, max_length=64)
    target_id: str | None = Field(default=None, max_length=128)

    correlation_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=128)

    reason: str | None = Field(default=None, max_length=4096)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None

    occurred_at: datetime = Field(default_factory=_utcnow)

    @field_validator("occurred_at")
    @classmethod
    def _normalise_utc(cls, value: datetime) -> datetime:
        """Coerce the logical timestamp to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _fill_and_check(self) -> AuditEvent:
        """Default the severity from the category and reject incoherent verbs."""
        if self.severity is None:
            object.__setattr__(self, "severity", default_severity(self.category))
        if not is_coherent(self.category, self.action):
            expected = category_for_action(self.action)
            raise ValueError(
                f"action {self.action.value!r} belongs to category {expected.value!r}, "
                f"not {self.category.value!r}"
            )
        return self

    # -- convenience constructors ------------------------------------------ #

    @classmethod
    def for_action(
        cls,
        action: AuditAction,
        *,
        actor_kind: AuditActorKind,
        actor_id: str,
        **kwargs: Any,
    ) -> AuditEvent:
        """Build an event, deriving ``category`` from ``action`` automatically."""
        return cls(
            category=category_for_action(action),
            action=action,
            actor_kind=actor_kind,
            actor_id=actor_id,
            **kwargs,
        )

    def occurred_at_iso(self) -> str:
        """The logical timestamp as a canonical ISO-8601 UTC string (for hashing)."""
        return self.occurred_at.isoformat()


__all__ = ["AuditEvent"]
