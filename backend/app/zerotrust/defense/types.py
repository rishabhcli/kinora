"""Canonical value types for the zero-trust defense subsystem.

The whole package speaks two small vocabularies:

* a **security event** — one normalized line of telemetry (an auth attempt, an
  access-log hit, an audit action) that detectors consume; and
* an **alert** — a scored, deduplicated finding a detector emits.

Both are immutable, hashable-friendly dataclasses with no framework imports, so
they serialise cleanly and replay deterministically. Enums are lowercase
:class:`enum.StrEnum` to match the rest of the data layer (JSON-portable).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


class EventKind(enum.StrEnum):
    """The category of a normalized security event."""

    #: A login / token-refresh / password-reset attempt (auth log).
    AUTH = "auth"
    #: An authorized request hitting a protected resource (access log).
    ACCESS = "access"
    #: A privileged or state-changing action (audit log).
    AUDIT = "audit"
    #: A raw inbound HTTP request being screened by the WAF.
    HTTP = "http"


class AuthOutcome(enum.StrEnum):
    """The result of an :data:`EventKind.AUTH` event."""

    SUCCESS = "success"
    FAILURE = "failure"
    #: Credentials were valid but a second factor / step-up is still required.
    CHALLENGE = "challenge"
    #: Account is locked out; the attempt never reached credential checking.
    LOCKED = "locked"


class Severity(enum.IntEnum):
    """Alert severity, ordered so ``>=`` thresholds read naturally.

    Kept as an :class:`IntEnum` (not StrEnum) so callers can compare and sort;
    :meth:`label` gives the portable lowercase string for serialisation.
    """

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def for_score(cls, score: float) -> Severity:
        """Map a normalized ``0..1`` anomaly score onto a severity band."""
        if score >= 0.9:
            return cls.CRITICAL
        if score >= 0.7:
            return cls.HIGH
        if score >= 0.4:
            return cls.MEDIUM
        if score >= 0.15:
            return cls.LOW
        return cls.INFO


class ThreatCategory(enum.StrEnum):
    """What kind of threat an alert represents (drives routing + dashboards)."""

    RATE_ANOMALY = "rate_anomaly"
    SEQUENCE_ANOMALY = "sequence_anomaly"
    BEHAVIORAL_ANOMALY = "behavioral_anomaly"
    CREDENTIAL_STUFFING = "credential_stuffing"
    ACCOUNT_TAKEOVER = "account_takeover"
    SCRAPING = "scraping"
    BRUTE_FORCE = "brute_force"
    WAF_BLOCK = "waf_block"
    BOT = "bot"
    SUPPLY_CHAIN = "supply_chain"


def _freeze(meta: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """Normalise optional metadata into a stable, hashable tuple of pairs."""
    if not meta:
        return ()
    return tuple(sorted(meta.items(), key=lambda kv: kv[0]))


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """One normalized line of security telemetry fed to the detectors.

    The fields are the intersection of what auth/access/audit logs carry that a
    detector actually keys on. ``principal`` is the acting subject (user id when
    known, else the source ip); ``target`` is the thing acted upon (a username
    being probed, a book id, a URL path). ``meta`` carries detector-specific
    extras (user-agent, status code, geo) without widening the core shape.
    """

    kind: EventKind
    ts: float
    """Wall-clock UNIX seconds the event occurred."""
    source_ip: str = "0.0.0.0"
    principal: str | None = None
    target: str | None = None
    action: str | None = None
    outcome: AuthOutcome | None = None
    user_agent: str | None = None
    status_code: int | None = None
    bytes_out: int = 0
    meta: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def auth(
        cls,
        *,
        ts: float,
        source_ip: str,
        username: str,
        outcome: AuthOutcome,
        user_agent: str | None = None,
        principal: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SecurityEvent:
        """Construct an auth-log event (``target`` is the probed username)."""
        return cls(
            kind=EventKind.AUTH,
            ts=ts,
            source_ip=source_ip,
            principal=principal,
            target=username,
            outcome=outcome,
            user_agent=user_agent,
            meta=_freeze(meta),
        )

    @classmethod
    def access(
        cls,
        *,
        ts: float,
        source_ip: str,
        principal: str | None,
        target: str,
        action: str = "GET",
        status_code: int = 200,
        bytes_out: int = 0,
        user_agent: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SecurityEvent:
        """Construct an access-log event (``target`` is the resource path)."""
        return cls(
            kind=EventKind.ACCESS,
            ts=ts,
            source_ip=source_ip,
            principal=principal,
            target=target,
            action=action,
            status_code=status_code,
            bytes_out=bytes_out,
            user_agent=user_agent,
            meta=_freeze(meta),
        )

    @classmethod
    def audit(
        cls,
        *,
        ts: float,
        source_ip: str,
        principal: str,
        action: str,
        target: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SecurityEvent:
        """Construct an audit-log event (a privileged action by ``principal``)."""
        return cls(
            kind=EventKind.AUDIT,
            ts=ts,
            source_ip=source_ip,
            principal=principal,
            action=action,
            target=target,
            meta=_freeze(meta),
        )

    @property
    def subject(self) -> str:
        """The best stable identity key: the principal, falling back to the ip."""
        return self.principal or self.source_ip

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self.meta:
            if k == key:
                return v
        return default

    def with_meta(self, **extra: Any) -> SecurityEvent:
        """Return a copy with additional metadata merged in (immutably)."""
        merged = dict(self.meta)
        merged.update(extra)
        return replace(self, meta=_freeze(merged))


@dataclass(frozen=True, slots=True)
class Alert:
    """A scored, attributed finding emitted by a detector.

    ``score`` is normalized to ``0..1`` (a calibrated anomaly probability-ish
    number, not a raw z-score); ``severity`` is the banded view of it.
    ``dedup_key`` lets the engine collapse a storm of identical findings into one
    alert with a rising count (see :mod:`.alerting`).
    """

    detector: str
    category: ThreatCategory
    severity: Severity
    score: float
    subject: str
    ts: float
    title: str
    description: str = ""
    source_ip: str | None = None
    evidence: tuple[tuple[str, Any], ...] = ()
    recommended_action: str | None = None
    dedup_key: str = ""
    count: int = 1
    #: ``None`` means "same as ``ts``"; resolved in ``__post_init__``. Using
    #: ``None`` rather than ``0.0`` as the sentinel matters: ``0.0`` is a valid
    #: (epoch / test-relative) timestamp and must not be silently overwritten.
    first_seen: float | None = None
    last_seen: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"alert score must be in [0,1], got {self.score!r}")
        if self.first_seen is None:
            object.__setattr__(self, "first_seen", self.ts)
        if self.last_seen is None:
            object.__setattr__(self, "last_seen", self.ts)
        if not self.dedup_key:
            object.__setattr__(self, "dedup_key", f"{self.detector}:{self.category}:{self.subject}")

    @property
    def first_at(self) -> float:
        """``first_seen`` guaranteed resolved to a float (never the None sentinel)."""
        return self.ts if self.first_seen is None else self.first_seen

    @property
    def last_at(self) -> float:
        """``last_seen`` guaranteed resolved to a float (never the None sentinel)."""
        return self.ts if self.last_seen is None else self.last_seen

    def evidence_get(self, key: str, default: Any = None) -> Any:
        for k, v in self.evidence:
            if k == key:
                return v
        return default

    def as_dict(self) -> dict[str, Any]:
        """A JSON-portable view for the store seam / dashboards."""
        return {
            "detector": self.detector,
            "category": str(self.category),
            "severity": self.severity.label,
            "severity_rank": int(self.severity),
            "score": round(self.score, 4),
            "subject": self.subject,
            "source_ip": self.source_ip,
            "ts": self.ts,
            "title": self.title,
            "description": self.description,
            "evidence": dict(self.evidence),
            "recommended_action": self.recommended_action,
            "dedup_key": self.dedup_key,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def make_evidence(**kv: Any) -> tuple[tuple[str, Any], ...]:
    """Helper to build the stable evidence tuple from keyword pairs."""
    return _freeze(kv)


__all__ = [
    "Alert",
    "AuthOutcome",
    "EventKind",
    "SecurityEvent",
    "Severity",
    "ThreatCategory",
    "make_evidence",
]
