"""Typed exception hierarchy for the query-optimization platform.

Every error the platform raises descends from :class:`OptimizeError`, so a caller
can ``except OptimizeError`` to catch anything from this package without also
swallowing unrelated ``ValueError``\\s. The hierarchy is intentionally small and
flat — each subclass marks a distinct, actionable failure mode.
"""

from __future__ import annotations


class OptimizeError(Exception):
    """Base class for every error raised by ``app.datascale.optimize``."""


class ParseError(OptimizeError):
    """A SQL statement could not be shaped into the structures we analyse.

    The shape parser is deliberately conservative: rather than mis-parse exotic
    syntax it raises this, and callers fall back to the un-optimised path.
    """


class RewriteUnsound(OptimizeError):  # noqa: N818 - reads as an adjective by design
    """A requested matview rewrite is not provably equivalent.

    Raised by the *strict* rewrite entrypoint. The default :func:`rewrite` API
    returns ``None`` instead of raising; this exists for callers that want a hard
    failure when they believe a rewrite *should* have applied (tests, asserts).
    """


class UnknownMatview(OptimizeError):  # noqa: N818 - names the missing entity
    """An operation referenced a materialized view not in the registry."""


class RefreshError(OptimizeError):
    """A materialized-view refresh could not be planned or executed."""


class RegressionDetected(OptimizeError):  # noqa: N818 - reads as a past participle
    """A plan regressed against its captured baseline beyond tolerance.

    Carries the structured :class:`~app.datascale.optimize.regression.PlanDiff`
    so a CI gate can print exactly what changed.
    """

    def __init__(self, message: str, *, diff: object | None = None) -> None:
        super().__init__(message)
        self.diff = diff


class CacheError(OptimizeError):
    """A result-cache operation failed (e.g. an un-hashable cache parameter)."""


__all__ = [
    "CacheError",
    "OptimizeError",
    "ParseError",
    "RefreshError",
    "RegressionDetected",
    "RewriteUnsound",
    "UnknownMatview",
]
