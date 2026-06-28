"""Kinora LLM-ops / prompt-registry platform (``backend/app/llmops/``).

A self-contained LLM-ops layer over the six-agent crew (see ``DESIGN.md``):

* a **versioned prompt registry** (register / diff / rollback the agents'
  ``VersionedPrompt``s with semver + an append-only changelog), seeded from
  ``app.agents.prompts`` without ever editing those prompts;
* a **prompt A/B + eval harness** (golden datasets scored against rubrics by a
  deterministic fake judge) with **regression detection** on prompt changes;
* a **prompt-injection + jailbreak defense filter** (input sanitization) and an
  **output policy** layer, composed into a **safety/guardrail** decision;
* a **model registry** + capability/cost routing config;
* **structured run-tracing** (prompt + inputs + outputs + tokens + cost) with a
  query API, and **response caching** keyed by prompt-version + inputs.

The platform never makes a live model call: the judge and embedder are injected
behind protocols, and the bundled defaults are deterministic fakes. Cites
kinora.md §10 (prompt contracts) and §13 (metrics / eval harness).
"""

from __future__ import annotations

from app.llmops.ab import ABResult, ABRunner
from app.llmops.cache import CacheStats, ResponseCache, cache_key
from app.llmops.datasets import DATASETS, GoldenCase, GoldenDataset, get_dataset
from app.llmops.diff import PromptDiff, diff_prompts, section_diff, suggest_bump
from app.llmops.errors import (
    DatasetError,
    DuplicateVersionError,
    GuardrailBlockedError,
    InvalidVersionError,
    LLMOpsError,
    ModelNotRegisteredError,
    NoCapableModelError,
    PromptNotFoundError,
    RollbackError,
    RubricError,
    TraceNotFoundError,
)
from app.llmops.guardrails import (
    Decision,
    GuardrailPolicy,
    InputVerdict,
    OutputVerdict,
    default_json_policy,
)
from app.llmops.harness import (
    EvalHarness,
    EvalReport,
    Responder,
    fake_responder,
    naive_responder,
)
from app.llmops.injection import InjectionScan, InjectionScanner, sanitize
from app.llmops.judge import HeuristicJudge, Judge, ModelBackedJudge
from app.llmops.models_registry import (
    Capability,
    Modality,
    ModelCard,
    ModelRegistry,
    RoutingRequest,
    default_catalog,
)
from app.llmops.output_policy import OutputPolicy, OutputReport, Severity
from app.llmops.registry import (
    ChangeKind,
    ChangelogEntry,
    PromptRecord,
    PromptRegistry,
    VersionStatus,
)
from app.llmops.regression import (
    RegressionPolicy,
    RegressionVerdict,
    detect,
    detect_from_harness,
)
from app.llmops.rubric import RUBRICS, Criterion, Rubric, ScoreResult, get_rubric, score
from app.llmops.semver import SemVer
from app.llmops.service import LLMOpsService
from app.llmops.tracing import (
    InMemoryTraceStore,
    RunTrace,
    TraceAggregate,
    TraceQuery,
    aggregate,
    group_by,
)

__all__ = [
    "ABResult",
    "ABRunner",
    "CacheStats",
    "Capability",
    "ChangeKind",
    "ChangelogEntry",
    "Criterion",
    "DATASETS",
    "DatasetError",
    "Decision",
    "DuplicateVersionError",
    "EvalHarness",
    "EvalReport",
    "GoldenCase",
    "GoldenDataset",
    "GuardrailBlockedError",
    "GuardrailPolicy",
    "HeuristicJudge",
    "InMemoryTraceStore",
    "InputVerdict",
    "InjectionScan",
    "InjectionScanner",
    "InvalidVersionError",
    "Judge",
    "LLMOpsError",
    "LLMOpsService",
    "ModelBackedJudge",
    "ModelCard",
    "ModelNotRegisteredError",
    "ModelRegistry",
    "Modality",
    "NoCapableModelError",
    "OutputPolicy",
    "OutputReport",
    "OutputVerdict",
    "PromptDiff",
    "PromptNotFoundError",
    "PromptRecord",
    "PromptRegistry",
    "RUBRICS",
    "RegressionPolicy",
    "RegressionVerdict",
    "Responder",
    "RollbackError",
    "RoutingRequest",
    "Rubric",
    "RubricError",
    "RunTrace",
    "ScoreResult",
    "SemVer",
    "Severity",
    "TraceAggregate",
    "TraceNotFoundError",
    "TraceQuery",
    "VersionStatus",
    "aggregate",
    "cache_key",
    "default_catalog",
    "default_json_policy",
    "detect",
    "detect_from_harness",
    "diff_prompts",
    "fake_responder",
    "get_dataset",
    "get_rubric",
    "group_by",
    "naive_responder",
    "sanitize",
    "score",
    "section_diff",
    "suggest_bump",
    "ResponseCache",
]
