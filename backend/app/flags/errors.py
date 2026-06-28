"""Typed error hierarchy for the feature-flags platform.

The *evaluation* path never raises into the caller — it always returns a
reasoned :class:`~app.flags.models.Evaluation`. These errors are for the
*authoring* / persistence surfaces (admin API, store, serialization), where
"reject the bad write" is the correct behavior.
"""

from __future__ import annotations


class FlagError(Exception):
    """Base class for every feature-flags error."""


class FlagValidationError(FlagError):
    """A flag/experiment definition is structurally invalid.

    Raised by the model constructors and the serialization layer when a write
    would produce an unevaluable definition (e.g. a rule pointing at a missing
    variation, rollout weights that do not sum to 100%, a duplicate key).
    """


class FlagNotFoundError(FlagError):
    """A flag key was requested from the store but does not exist."""


class ExperimentError(FlagError):
    """Base class for experiment-definition errors."""


class ExperimentValidationError(ExperimentError, FlagValidationError):
    """An experiment definition is structurally invalid."""


class StatsError(FlagError):
    """A statistical computation received inconsistent / impossible inputs."""


__all__ = [
    "ExperimentError",
    "ExperimentValidationError",
    "FlagError",
    "FlagNotFoundError",
    "FlagValidationError",
    "StatsError",
]
