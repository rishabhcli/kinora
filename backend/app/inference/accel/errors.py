"""Typed error family for the inference acceleration layer.

These are deliberately distinct from :mod:`app.providers.errors` (the transport
faults). Accel errors describe *orchestration* failures — a fan-out that
exhausted its cost cap before any provider returned a good answer, a constrained
decode that could not satisfy its schema, a speculative round whose target
disagreed with its own committed prefix. The provider faults bubble up
unchanged underneath them.
"""

from __future__ import annotations


class AccelError(Exception):
    """Base class for every inference-acceleration orchestration error."""


class FanOutExhaustedError(AccelError):
    """Every fan-out candidate failed (or was vetoed) before a good answer arrived.

    Attributes:
        attempts: How many providers were actually started.
        cost_spent: Total cost units charged across the started candidates.
        last_error: The most recent underlying failure, if any candidate raised.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        cost_spent: float = 0.0,
        last_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.cost_spent = cost_spent
        self.last_error = last_error


class CostCapExceededError(AccelError):
    """A fan-out could not even start a candidate without breaching the cost cap."""

    def __init__(self, message: str, *, cost_cap: float, would_spend: float) -> None:
        super().__init__(message)
        self.cost_cap = cost_cap
        self.would_spend = would_spend


class ConstrainedDecodeError(AccelError):
    """A constrained generation could not produce output satisfying its constraint.

    Attributes:
        raw_text: The last raw model text that failed validation, for debugging.
        attempts: How many repair rounds were spent.
    """

    def __init__(self, message: str, *, raw_text: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.attempts = attempts


class SpeculationConsistencyError(AccelError):
    """The target model disagreed with a token it had *already committed*.

    Speculative decoding must be exactly equivalent to plain target decoding; if
    a verification pass ever contradicts an accepted prefix the orchestrator
    refuses to proceed rather than emit a divergent result. Hitting this is a
    bug in a backend, never normal operation.
    """


class CalibrationError(AccelError):
    """Threshold calibration was asked for something it cannot compute.

    e.g. an empty labelled set, or a target precision/recall that no threshold in
    the candidate scores can satisfy.
    """


__all__ = [
    "AccelError",
    "CalibrationError",
    "ConstrainedDecodeError",
    "CostCapExceededError",
    "FanOutExhaustedError",
    "SpeculationConsistencyError",
]
