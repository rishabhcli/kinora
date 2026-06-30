"""The separate, zero-by-default eval budget for candidate (shadow) renders.

The reader's render budget (§11) pays for what the reader actually sees. Shadow
renders are off that critical path and **must never draw it down** — a candidate
evaluation that spent the reader's scarce video-seconds would degrade the live
film. So candidate spend is metered here, against a *distinct* pool that:

* **Defaults to ZERO.** Construct it with no funding and every reservation is
  refused (:class:`EvalBudgetExhausted`) — so merely turning shadow mode on can
  never spend a real video-second. Spending requires an operator to *explicitly*
  fund the pool (``cap_video_seconds > 0``), which in turn still only matters when
  ``KINORA_LIVE_VIDEO`` is on at the provider layer.
* **Is a hard cap.** Reservations are checked against committed spend; an
  over-budget reservation is refused atomically (no partial spend).
* **Settles to measured spend.** A reservation is for the shot's *expected*
  seconds; once the render returns its *measured* seconds, the reservation is
  reconciled so the cap reflects reality, not the estimate.

This object is in-memory and single-process by design (the harness runs in the API
process alongside the scheduler); a durable adapter can replace it later behind the
same surface. It is intentionally **not** the reader budget service and shares no
state with it.
"""

from __future__ import annotations

from dataclasses import dataclass


class EvalBudgetError(RuntimeError):
    """Base class for eval-budget refusals."""


class EvalBudgetExhausted(EvalBudgetError):  # noqa: N818 - descriptive name; parent carries Error
    """Raised when a reservation would exceed the funded eval cap.

    The default (zero-funded) budget raises this for *every* reservation — the
    guarantee that turning shadow mode on never spends a real video-second.
    """

    def __init__(self, requested: float, remaining: float, cap: float) -> None:
        self.requested = requested
        self.remaining = remaining
        self.cap = cap
        super().__init__(
            f"eval budget exhausted: requested {requested:.3f}s but only "
            f"{remaining:.3f}s of {cap:.3f}s remain"
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """A held claim on eval video-seconds, settled once the render is measured."""

    shot_id: str
    reserved_s: float


@dataclass(frozen=True, slots=True)
class EvalBudgetSnapshot:
    """An immutable read of the eval budget's accounting."""

    cap_video_seconds: float
    committed_video_seconds: float
    reserved_video_seconds: float

    @property
    def remaining_video_seconds(self) -> float:
        """Seconds still available (cap minus committed minus held reservations)."""
        return max(
            0.0,
            self.cap_video_seconds - self.committed_video_seconds - self.reserved_video_seconds,
        )

    @property
    def is_funded(self) -> bool:
        """True iff the operator funded any candidate spend at all."""
        return self.cap_video_seconds > 0.0


class EvalBudget:
    """A hard-capped, zero-by-default pool of candidate-render video-seconds."""

    def __init__(self, cap_video_seconds: float = 0.0) -> None:
        #: Total candidate video-seconds the operator funded. ``0.0`` (default) ⇒
        #: nothing may ever be reserved ⇒ shadow mode cannot spend.
        self._cap = max(0.0, float(cap_video_seconds))
        self._committed = 0.0
        self._reserved = 0.0

    @property
    def is_funded(self) -> bool:
        """True iff any candidate spend was explicitly funded."""
        return self._cap > 0.0

    def remaining(self) -> float:
        """Currently available eval video-seconds."""
        return max(0.0, self._cap - self._committed - self._reserved)

    def can_reserve(self, video_seconds: float) -> bool:
        """True iff ``video_seconds`` could be reserved right now (no mutation)."""
        want = max(0.0, float(video_seconds))
        if want <= 0.0:
            # A zero-cost reservation is always admissible (gated/degraded renders
            # still get recorded), but only when the pool is funded — an unfunded
            # pool admits nothing so the zero-by-default guard is total.
            return self.is_funded
        return self.remaining() >= want

    def reserve(self, shot_id: str, video_seconds: float) -> Reservation:
        """Hold ``video_seconds`` for ``shot_id`` or refuse atomically.

        Raises :class:`EvalBudgetExhausted` if the pool is unfunded or would be
        overdrawn. On success the seconds move into the *reserved* pool until
        :meth:`settle` reconciles them against the measured spend.
        """
        want = max(0.0, float(video_seconds))
        if not self.can_reserve(want):
            raise EvalBudgetExhausted(want, self.remaining(), self._cap)
        self._reserved += want
        return Reservation(shot_id=shot_id, reserved_s=want)

    def settle(self, reservation: Reservation, measured_video_seconds: float) -> None:
        """Reconcile a held reservation against the render's *measured* spend.

        Releases the held estimate and commits the measured seconds. The measured
        amount is clamped at zero (a failed/gated render bills nothing); it may
        legitimately differ from the estimate (provider rounded the clip length).
        """
        measured = max(0.0, float(measured_video_seconds))
        self._reserved = max(0.0, self._reserved - reservation.reserved_s)
        self._committed += measured

    def release(self, reservation: Reservation) -> None:
        """Release a reservation that never rendered (spend nothing)."""
        self._reserved = max(0.0, self._reserved - reservation.reserved_s)

    def snapshot(self) -> EvalBudgetSnapshot:
        """An immutable view of the current accounting."""
        return EvalBudgetSnapshot(
            cap_video_seconds=self._cap,
            committed_video_seconds=self._committed,
            reserved_video_seconds=self._reserved,
        )


__all__ = [
    "EvalBudget",
    "EvalBudgetError",
    "EvalBudgetExhausted",
    "EvalBudgetSnapshot",
    "Reservation",
]
