"""Inference **acceleration** — facet B of the Kinora inference gateway.

A self-contained, deterministic-testable layer that sits *on top of* an
:class:`~app.inference.accel.protocol.InferenceBackend` and makes generation
cheaper and faster without changing its output:

================  ==========================================================
Component         What it does
================  ==========================================================
``speculative``   Draft proposes, target verifies; accept the longest correct
                  prefix + a free correction token. Adaptive draft length from
                  a tracked acceptance rate. Output is *identical* to plain
                  target decoding (proven by tests).
``semantic_cache``Exact-prefix + embedding-similarity response cache with
                  calibrated thresholds, staleness/versioning, hit-rate metrics.
``calibration``   Turns labelled (equivalent / not) prompt pairs into a cosine
                  threshold meeting a target precision.
``prefix_reuse``  Prompt-prefix / KV-block reuse bookkeeping over a prefix trie;
                  reports how many prompt tokens were served from reused KV.
``fanout``        Multi-provider first-good-wins racing with hard cost caps,
                  staggered hedging, and answer validators.
``constrained``   JSON-schema / regex / choice constrained-output decoding
                  helpers with token-mask projection and bounded repair.
================  ==========================================================

Nothing here makes a live model call by itself. The deterministic
:mod:`~app.inference.accel.fakes` back every test; production wires the real
DashScope transport behind the :class:`InferenceBackend` protocol.
"""

from __future__ import annotations

from .adapters import ChatBackend, EmbeddingAdapter
from .batching import (
    BatcherStats,
    CoalescerStats,
    MicroBatcher,
    RequestCoalescer,
    batch_from_single,
)
from .calibration import (
    CalibrationResult,
    LabeledPair,
    calibrate_threshold,
    evaluate_threshold,
)
from .clock import SYSTEM_CLOCK, Clock, FakeClock, SystemClock
from .constrained import (
    ChoiceConstraint,
    ConstrainedDecoder,
    ConstrainedResult,
    Constraint,
    JsonSchemaConstraint,
    JsonValueConstraint,
    RegexConstraint,
    ValidationResult,
    constrain,
    project_mask,
)
from .errors import (
    AccelError,
    CalibrationError,
    ConstrainedDecodeError,
    CostCapExceededError,
    FanOutExhaustedError,
    SpeculationConsistencyError,
)
from .fanout import (
    FanOutRacer,
    FanOutResult,
    ProviderCandidate,
    first_good,
)
from .gateway import AcceleratedGateway
from .metrics import (
    CacheMetrics,
    CacheSnapshot,
    FanOutMetrics,
    FanOutSnapshot,
    PrefixReuseMetrics,
    PrefixReuseSnapshot,
    SpeculativeMetrics,
    SpeculativeSnapshot,
)
from .prefix_reuse import KVReuseBook, PrefixTrie, ReusePlan
from .protocol import (
    DraftBackend,
    Embedder,
    GenerationRequest,
    GenerationResult,
    InferenceBackend,
    TokenProposal,
    TokenScorer,
)
from .semantic_cache import (
    CacheConfig,
    CacheEntry,
    LookupOutcome,
    SemanticCache,
    cosine,
    exact_key,
)
from .speculative import (
    AdaptiveConfig,
    AdaptiveDraftLength,
    SpeculativeDecoder,
    SpeculativeDecodeResult,
    speculative_text,
)

__all__ = [
    "SYSTEM_CLOCK",
    "AccelError",
    "AcceleratedGateway",
    "AdaptiveConfig",
    "AdaptiveDraftLength",
    "BatcherStats",
    "CacheConfig",
    "CacheEntry",
    "CacheMetrics",
    "CacheSnapshot",
    "CalibrationError",
    "CalibrationResult",
    "ChatBackend",
    "ChoiceConstraint",
    "Clock",
    "CoalescerStats",
    "ConstrainedDecodeError",
    "ConstrainedDecoder",
    "ConstrainedResult",
    "Constraint",
    "CostCapExceededError",
    "DraftBackend",
    "Embedder",
    "EmbeddingAdapter",
    "FakeClock",
    "FanOutExhaustedError",
    "FanOutMetrics",
    "FanOutRacer",
    "FanOutResult",
    "FanOutSnapshot",
    "GenerationRequest",
    "GenerationResult",
    "InferenceBackend",
    "JsonSchemaConstraint",
    "JsonValueConstraint",
    "KVReuseBook",
    "LabeledPair",
    "LookupOutcome",
    "MicroBatcher",
    "PrefixReuseMetrics",
    "PrefixReuseSnapshot",
    "PrefixTrie",
    "ProviderCandidate",
    "RegexConstraint",
    "RequestCoalescer",
    "ReusePlan",
    "SemanticCache",
    "SpeculationConsistencyError",
    "SpeculativeDecodeResult",
    "SpeculativeDecoder",
    "SpeculativeMetrics",
    "SpeculativeSnapshot",
    "SystemClock",
    "TokenProposal",
    "TokenScorer",
    "ValidationResult",
    "batch_from_single",
    "calibrate_threshold",
    "constrain",
    "cosine",
    "evaluate_threshold",
    "exact_key",
    "first_good",
    "project_mask",
    "speculative_text",
]
