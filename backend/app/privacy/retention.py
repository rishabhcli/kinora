"""The retention-policy engine (per-data-class TTL + legal-hold exceptions).

A retention policy assigns each *retention class* (the named classes the data-map
tags fields with) a time-to-live. Given a record's creation time and the current
time, the engine decides whether the record should be :data:`RetentionAction.RETAIN`\\
ed or has aged out and is eligible to :data:`~RetentionAction.EXPIRE`. Two
exceptions override expiry:

* **Legal hold** — an active hold over a subject (or a whole data class) blocks
  *all* deletion for the held scope, returning
  :data:`~RetentionAction.BLOCKED_BY_HOLD`. Litigation / regulatory holds must
  win over both TTL expiry and an explicit erasure request (the orchestrator
  consults the engine before any destructive step).
* **Consent withdrawal** — withdrawing consent for a purpose can shorten a class's
  effective TTL to zero (the data loses its lawful basis), making it immediately
  expiry-eligible — unless a hold blocks it.

Pure policy: the engine takes plain values + an injectable clock, so the whole
TTL / hold / consent matrix is deterministically unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.privacy.clock import Clock, ensure_utc, system_clock
from app.privacy.enums import RetentionAction


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """The TTL + lawful-basis note for one retention class.

    ``ttl_days=None`` means *keep for the life of the account* (no automatic
    expiry); a positive value expires records ``ttl_days`` after creation.
    ``consent_purpose`` links the class to a consent purpose: withdrawing that
    purpose removes the lawful basis and shortens the effective TTL to zero.
    """

    data_class: str
    ttl_days: int | None
    lawful_basis: str = "contract"
    consent_purpose: str | None = None
    description: str = ""

    def expires_at(self, created_at: datetime) -> datetime | None:
        """When a record created at ``created_at`` ages out (None == never)."""
        if self.ttl_days is None:
            return None
        return ensure_utc(created_at) + timedelta(days=self.ttl_days)


@dataclass(frozen=True, slots=True)
class LegalHold:
    """A litigation/regulatory hold that suspends deletion for its scope.

    A hold is scoped to a ``subject_id`` and optionally narrowed to a single
    ``data_class`` (a class-wide hold leaves ``data_class=None``). While
    :attr:`active`, the engine refuses expiry/erasure for the held scope.
    """

    id: str
    subject_id: str
    data_class: str | None = None
    reason: str = ""
    active: bool = True
    placed_at: datetime | None = None

    def covers(self, *, subject_id: str, data_class: str) -> bool:
        """Whether this hold (if active) blocks deletion of ``data_class`` for the subject."""
        if not self.active or self.subject_id != subject_id:
            return False
        return self.data_class is None or self.data_class == data_class


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """The engine's verdict for one (data class, record) at evaluation time."""

    data_class: str
    action: RetentionAction
    expires_at: datetime | None
    #: The blocking hold id when ``action`` is ``BLOCKED_BY_HOLD``.
    hold_id: str | None = None
    reason: str = ""


@dataclass
class RetentionPolicy:
    """A set of :class:`RetentionRule`\\ s, evaluated against holds + consent."""

    rules: dict[str, RetentionRule] = field(default_factory=dict)
    clock: Clock = system_clock

    @classmethod
    def from_rules(
        cls, rules: Iterable[RetentionRule], *, clock: Clock = system_clock
    ) -> RetentionPolicy:
        """Build a policy from a list of rules (keyed by data class)."""
        return cls(rules={r.data_class: r for r in rules}, clock=clock)

    def rule(self, data_class: str) -> RetentionRule | None:
        """The rule governing ``data_class`` (None if unmanaged)."""
        return self.rules.get(data_class)

    def evaluate(
        self,
        *,
        data_class: str,
        subject_id: str,
        created_at: datetime,
        holds: Iterable[LegalHold] = (),
        consent_withdrawn: bool = False,
    ) -> RetentionDecision:
        """Decide RETAIN / EXPIRE / BLOCKED_BY_HOLD for one record.

        Hold wins over everything; otherwise a withdrawn-consent class or an
        aged-out TTL expires, and a still-live TTL retains.
        """
        rule = self.rules.get(data_class)
        # A hold always blocks deletion, even past TTL or after consent withdrawal.
        for h in holds:
            if h.covers(subject_id=subject_id, data_class=data_class):
                return RetentionDecision(
                    data_class=data_class,
                    action=RetentionAction.BLOCKED_BY_HOLD,
                    expires_at=rule.expires_at(created_at) if rule else None,
                    hold_id=h.id,
                    reason="active legal hold",
                )
        if rule is None:
            # Unmanaged class: no TTL, retain by default (an explicit erasure can
            # still target it — that path checks holds separately).
            return RetentionDecision(
                data_class=data_class,
                action=RetentionAction.RETAIN,
                expires_at=None,
                reason="no retention rule (unmanaged)",
            )
        if consent_withdrawn and rule.consent_purpose is not None:
            return RetentionDecision(
                data_class=data_class,
                action=RetentionAction.EXPIRE,
                expires_at=ensure_utc(created_at),
                reason=f"consent withdrawn for {rule.consent_purpose!r}",
            )
        expires_at = rule.expires_at(created_at)
        if expires_at is None:
            return RetentionDecision(
                data_class=data_class,
                action=RetentionAction.RETAIN,
                expires_at=None,
                reason="no TTL (retained for account lifetime)",
            )
        now = self.clock()
        if expires_at <= now:
            return RetentionDecision(
                data_class=data_class,
                action=RetentionAction.EXPIRE,
                expires_at=expires_at,
                reason="TTL elapsed",
            )
        return RetentionDecision(
            data_class=data_class,
            action=RetentionAction.RETAIN,
            expires_at=expires_at,
            reason="within TTL",
        )

    def is_blocked(
        self, *, subject_id: str, data_class: str, holds: Iterable[LegalHold]
    ) -> LegalHold | None:
        """Return the active hold blocking ``data_class`` for the subject (or None).

        The erasure orchestrator calls this directly before a destructive step so a
        held class is never touched even when an explicit erasure request arrives.
        """
        for h in holds:
            if h.covers(subject_id=subject_id, data_class=data_class):
                return h
        return None


# --------------------------------------------------------------------------- #
# Kinora's default retention policy. The data classes match the data-map's     #
# retention_class names (datamap.RC_*); TTLs are the platform defaults a DPO   #
# would set and are overridable via settings (see app.privacy.settings).       #
# --------------------------------------------------------------------------- #

_DEFAULT_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        data_class="account",
        ttl_days=None,  # life of the account
        lawful_basis="contract",
        description="Account row — kept while the account exists.",
    ),
    RetentionRule(
        data_class="uploaded_book",
        ttl_days=None,
        lawful_basis="contract",
        consent_purpose="adaptation",
        description="Source PDF — kept while consent to adapt it stands.",
    ),
    RetentionRule(
        data_class="generated_media",
        ttl_days=None,
        lawful_basis="contract",
        consent_purpose="adaptation",
        description="Clips/keyframes/narration derived from the book.",
    ),
    RetentionRule(
        data_class="reading_session",
        ttl_days=365,
        lawful_basis="legitimate_interests",
        consent_purpose="analytics",
        description="Behavioural reading sessions — 1y rolling window.",
    ),
    RetentionRule(
        data_class="directing_preference",
        ttl_days=730,
        lawful_basis="consent",
        consent_purpose="personalization",
        description="Learned directing-style profile (§8.6).",
    ),
    RetentionRule(
        data_class="audit_log",
        ttl_days=2555,  # ~7y accountability window
        lawful_basis="legal_obligation",
        description="Security/audit entries — long accountability retention.",
    ),
    RetentionRule(
        data_class="event_stream",
        ttl_days=2555,
        lawful_basis="legal_obligation",
        description="Append-only domain events — long retention, crypto-erased on RTBF.",
    ),
)


def default_retention_policy(
    *,
    overrides: Mapping[str, int | None] | None = None,
    clock: Clock = system_clock,
) -> RetentionPolicy:
    """Kinora's default retention policy, with optional per-class TTL overrides."""
    rules: list[RetentionRule] = []
    for r in _DEFAULT_RULES:
        if overrides and r.data_class in overrides:
            r = RetentionRule(
                data_class=r.data_class,
                ttl_days=overrides[r.data_class],
                lawful_basis=r.lawful_basis,
                consent_purpose=r.consent_purpose,
                description=r.description,
            )
        rules.append(r)
    return RetentionPolicy.from_rules(rules, clock=clock)


__all__ = [
    "LegalHold",
    "RetentionDecision",
    "RetentionPolicy",
    "RetentionRule",
    "default_retention_policy",
]
