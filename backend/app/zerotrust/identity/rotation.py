"""SVID rotation — keeping a workload's credential fresh before it expires.

SPIFFE credentials are deliberately short-lived, so a workload must rotate *ahead
of expiry* (it can never afford to discover its cert is dead mid-request). This
module is the workload-side seam for that:

* :class:`RotationPolicy` decides *when* a credential is due for renewal — at a
  fraction of its lifetime elapsed (the SPIFFE default is rotate at ~50% of TTL),
  clamped so a clock that overshoots still rotates.
* :class:`WorkloadIdentitySource` is the SDK-style handle a workload holds: it
  caches the current X.509-SVID and, on :meth:`current` / :meth:`refresh`,
  re-issues from the :class:`IdentityIssuer` whenever the policy says it's due.
  This is the X.509 analogue of go-spiffe's ``workloadapi.X509Source``.

All time decisions go through the issuer's clock, so a test can ``ManualClock``
its way across a credential's whole lifetime and assert exactly one rotation per
window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.zerotrust.identity.attestation import AttestationResult
from app.zerotrust.identity.clock import Clock
from app.zerotrust.identity.issuer import IdentityIssuer
from app.zerotrust.identity.spiffe import SpiffeId
from app.zerotrust.identity.svid import X509Svid


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    """When to renew a credential, expressed as a fraction of its lifetime."""

    #: rotate once this fraction of [notBefore, notAfter] has elapsed
    renew_at_fraction: float = 0.5
    #: never let remaining lifetime fall below this absolute floor
    min_remaining: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if not 0.0 < self.renew_at_fraction < 1.0:
            raise ValueError("renew_at_fraction must be in (0, 1)")

    def renew_after(self, svid: X509Svid) -> datetime:
        """The instant at-or-after which *svid* should be renewed."""

        lifetime = svid.not_after - svid.not_before
        threshold = svid.not_before + lifetime * self.renew_at_fraction
        floor = svid.not_after - self.min_remaining
        # rotate at the earlier of (fraction elapsed) and (min_remaining left)
        return min(threshold, floor)

    def is_due(self, svid: X509Svid, now: datetime) -> bool:
        """Whether *svid* should be rotated as of *now*."""

        return now >= self.renew_after(svid)


@dataclass(slots=True)
class RotationEvent:
    """A record of one rotation (old → new), for audit/metrics."""

    spiffe_id: SpiffeId
    at: datetime
    old_serial: int | None
    new_serial: int


@dataclass(slots=True)
class WorkloadIdentitySource:
    """A workload's live handle to its own auto-rotating X.509-SVID."""

    issuer: IdentityIssuer
    spiffe_id: SpiffeId
    policy: RotationPolicy = field(default_factory=RotationPolicy)
    _svid: X509Svid | None = None
    _history: list[RotationEvent] = field(default_factory=list)
    #: optional sink notified on every rotation (audit hook)
    on_rotate: Callable[[RotationEvent], None] | None = None

    @property
    def clock(self) -> Clock:
        return self.issuer.clock

    @classmethod
    def for_attestation(
        cls,
        issuer: IdentityIssuer,
        attestation: AttestationResult,
        *,
        policy: RotationPolicy | None = None,
        on_rotate: Callable[[RotationEvent], None] | None = None,
    ) -> WorkloadIdentitySource:
        """Build a source for the identity an attestation resolves to."""

        entry = issuer.registry.require_match(attestation)
        return cls(
            issuer=issuer,
            spiffe_id=entry.spiffe_id,
            policy=policy or RotationPolicy(),
            on_rotate=on_rotate,
        )

    def current(self) -> X509Svid:
        """Return the current SVID, rotating first if the policy says it's due."""

        now = self.clock.now()
        if self._svid is None or self.policy.is_due(self._svid, now):
            self._rotate(now)
        assert self._svid is not None
        return self._svid

    def refresh(self) -> X509Svid:
        """Force an immediate rotation regardless of policy."""

        self._rotate(self.clock.now())
        assert self._svid is not None
        return self._svid

    def peek(self) -> X509Svid | None:
        """The cached SVID without triggering a rotation (``None`` if unissued)."""

        return self._svid

    def needs_rotation(self) -> bool:
        if self._svid is None:
            return True
        return self.policy.is_due(self._svid, self.clock.now())

    def history(self) -> tuple[RotationEvent, ...]:
        return tuple(self._history)

    # -- internals --------------------------------------------------------- #
    def _rotate(self, now: datetime) -> None:
        old_serial = self._svid.serial_number if self._svid is not None else None
        new = self.issuer.issue_for_id(self.spiffe_id)
        self._svid = new
        event = RotationEvent(
            spiffe_id=self.spiffe_id,
            at=now,
            old_serial=old_serial,
            new_serial=new.serial_number,
        )
        self._history.append(event)
        if self.on_rotate is not None:
            self.on_rotate(event)


__all__ = [
    "RotationEvent",
    "RotationPolicy",
    "WorkloadIdentitySource",
]
