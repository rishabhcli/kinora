"""Kinora feature-flags & experimentation platform.

A flag + A/B experimentation system with a **pure, deterministic evaluator that
needs no infrastructure** at its core, wrapped by optional Postgres persistence,
a Redis-streamed evaluation cache, an admin/eval API, and an SDK-style client.

The public entrypoints most callers want:

* :class:`FlagEvaluator` + :class:`FlagSnapshot` + :class:`EvalContext` — evaluate
  flags in-process with zero I/O.
* :class:`FlagsClient` — the SDK facade (``bool_variation`` / ``string_variation``
  / ``variation``) with exposure logging.
* :class:`Flag` and its ``boolean`` / ``multivariate`` constructors — author flags.

See ``DESIGN.md`` for the full architecture and kinora.md §13 for the eval-harness
context this platform powers.
"""

from __future__ import annotations

from app.flags.client import ExposureSink, FlagsClient
from app.flags.context import EvalContext
from app.flags.errors import (
    ExperimentValidationError,
    FlagError,
    FlagNotFoundError,
    FlagValidationError,
    StatsError,
)
from app.flags.evaluator import FlagEvaluator
from app.flags.experiment import (
    Assignment,
    Experiment,
    ExperimentEngine,
    ExperimentStatus,
    Metric,
    MetricDirection,
    Variant,
)
from app.flags.models import (
    EMPTY_SNAPSHOT,
    Clause,
    Evaluation,
    Flag,
    FlagKind,
    FlagSnapshot,
    Operator,
    Prerequisite,
    Reason,
    Rollout,
    Rule,
    Target,
    Variation,
    WeightedVariation,
)
from app.flags.report import (
    ArmComparison,
    ExperimentReport,
    GuardrailResult,
    Recommendation,
    build_report,
)
from app.flags.stats import (
    AlwaysValidResult,
    ProportionStat,
    SampleStat,
    msprt_proportion,
    two_proportion_ztest,
    welch_ttest,
)

__all__ = [
    "EMPTY_SNAPSHOT",
    "AlwaysValidResult",
    "ArmComparison",
    "Assignment",
    "Clause",
    "EvalContext",
    "Evaluation",
    "Experiment",
    "ExperimentEngine",
    "ExperimentReport",
    "ExperimentStatus",
    "ExperimentValidationError",
    "ExposureSink",
    "Flag",
    "FlagError",
    "FlagEvaluator",
    "FlagKind",
    "FlagNotFoundError",
    "FlagSnapshot",
    "FlagValidationError",
    "FlagsClient",
    "GuardrailResult",
    "Metric",
    "MetricDirection",
    "Operator",
    "Prerequisite",
    "ProportionStat",
    "Reason",
    "Recommendation",
    "Rollout",
    "Rule",
    "SampleStat",
    "StatsError",
    "Target",
    "Variant",
    "Variation",
    "WeightedVariation",
    "build_report",
    "msprt_proportion",
    "two_proportion_ztest",
    "welch_ttest",
]
