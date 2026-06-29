"""Exception hierarchy for the alignment / preference-optimization platform.

A single root (:class:`AlignmentError`) so callers can ``except AlignmentError``
to catch anything this package raises, with specific subclasses for the distinct
failure modes (bad data, non-convergence, guardrail trips, orchestration faults).
"""

from __future__ import annotations


class AlignmentError(Exception):
    """Root of every error raised by ``app.mlplatform.alignment``."""


class DataError(AlignmentError):
    """A dataset / sample is malformed, empty, or internally inconsistent.

    Raised for shape mismatches, NaN / inf in features, an empty training set,
    or a preference pair whose two sides are identical.
    """


class NotFittedError(AlignmentError):
    """A model was asked to predict / score before it was fit."""


class ConvergenceError(AlignmentError):
    """An iterative optimizer failed to converge within its iteration budget.

    Only raised when the caller opts into strict convergence; by default the
    optimizers return their best iterate and surface a non-converged flag.
    """


class GuardrailTripped(AlignmentError):  # noqa: N818 - reads as a guardrail signal
    """A policy-evaluation guardrail (KL / over-optimization) blocked a release.

    Carries the structured :class:`~app.mlplatform.alignment.policy.GuardrailReport`
    that explains *which* guardrail tripped and by how much, so the caller can log
    or surface it without re-deriving the reason.
    """

    def __init__(self, message: str, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class OrchestrationError(AlignmentError):
    """A fine-tuning job could not be created, transitioned, or executed.

    Includes illegal lifecycle transitions, unknown providers, and executor
    faults surfaced by the (faked) backend.
    """


class ExperimentError(AlignmentError):
    """An experiment-tracking operation was illegal (dup id, unknown run, …)."""
