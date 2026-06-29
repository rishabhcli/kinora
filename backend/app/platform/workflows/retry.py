"""Activity retry policy — the durable, deterministic version.

Mirrors the shape of :class:`app.jobs.backoff.BackoffPolicy` and the render
queue's policy, but lives here because the durable engine needs the policy to be
(a) **JSON-serialisable** (it's recorded in ``ActivityScheduled`` so a replay
recomputes the same schedule) and (b) **deterministic** — the backoff is
computed without jitter by default so that a crash mid-retry resumes on the exact
same delay. (Jitter would make the *scheduled* fire time non-deterministic; the
engine instead spreads load at the worker/dispatch layer, not in workflow time.)

The policy classifies a failure into RETRY vs. give-up using both the attempt
count and the error's ``non_retryable`` flag / type allow-list, then computes the
next delay as ``initial * backoff**(attempt-1)`` capped at ``maximum``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential-backoff retry policy for an activity (deterministic).

    ``maximum_attempts`` of 0 means *unlimited* (retry until a non-retryable
    error or a schedule-to-close timeout intervenes). ``non_retryable_types`` is
    an allow-list of :class:`ApplicationError.type` tags that skip retry even when
    attempts remain.
    """

    initial_interval_s: float = 1.0
    backoff_coefficient: float = 2.0
    maximum_interval_s: float = 100.0
    maximum_attempts: int = 3
    non_retryable_types: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.initial_interval_s < 0 or self.maximum_interval_s < 0:
            raise ValueError("intervals must be non-negative")
        if self.backoff_coefficient < 1.0:
            raise ValueError("backoff_coefficient must be >= 1.0")
        if self.maximum_attempts < 0:
            raise ValueError("maximum_attempts must be >= 0 (0 == unlimited)")

    def should_retry(self, *, attempt: int, non_retryable: bool, error_type: str | None) -> bool:
        """Decide whether attempt ``attempt`` (1-based, already failed) retries."""
        if non_retryable:
            return False
        if error_type is not None and error_type in self.non_retryable_types:
            return False
        if self.maximum_attempts == 0:
            return True
        return attempt < self.maximum_attempts

    def delay_for(self, *, next_attempt: int) -> float:
        """Backoff delay (seconds) before ``next_attempt`` (1-based; >=2 here)."""
        if next_attempt <= 1:
            return 0.0
        delay = self.initial_interval_s * (self.backoff_coefficient ** (next_attempt - 2))
        return min(delay, self.maximum_interval_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_interval_s": self.initial_interval_s,
            "backoff_coefficient": self.backoff_coefficient,
            "maximum_interval_s": self.maximum_interval_s,
            "maximum_attempts": self.maximum_attempts,
            "non_retryable_types": list(self.non_retryable_types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetryPolicy:
        return cls(
            initial_interval_s=float(data.get("initial_interval_s", 1.0)),
            backoff_coefficient=float(data.get("backoff_coefficient", 2.0)),
            maximum_interval_s=float(data.get("maximum_interval_s", 100.0)),
            maximum_attempts=int(data.get("maximum_attempts", 3)),
            non_retryable_types=tuple(data.get("non_retryable_types", ())),
        )


#: A sensible default mirroring the render queue's (1s, 2s, 4s) shape with 3 tries.
DEFAULT_RETRY_POLICY = RetryPolicy()


__all__ = ["DEFAULT_RETRY_POLICY", "RetryPolicy"]
