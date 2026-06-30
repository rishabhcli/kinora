"""Content-safety / moderation **gateway** for the generation pipeline.

Kinora turns *arbitrary reader-supplied books* into generated film, and the clips
come from many providers (DashScope Wan, MiniMax Hailuo, future hosted lanes) —
each with a **different content policy**. A scene that one model renders happily
another refuses outright, burning a metered video-second on a request that was
never going to succeed. This package puts **one gateway** in front of the whole
generation loop so a prompt *and* the generated video are checked the same way
regardless of which provider ultimately runs the render.

This is intentionally a *distinct* concern from :mod:`app.moderation` (which owns
human-review queues, takedown/appeal state machines, repeat-offender escalation,
and the tamper-evident moderation audit log over the **source book** and **shown
output**). :mod:`app.safety` sits *earlier and tighter* in the render path and
owns the generation-time decisions the pipeline makes on every shot:

* :mod:`.taxonomy` — the safety vocabulary: policy categories, ordered severity,
  and the four gateway actions (``ALLOW`` / ``TRANSFORM`` / ``QUARANTINE`` /
  ``BLOCK``). Pure, importable anywhere.
* :mod:`.contracts` — typed, immutable value objects threaded through every layer
  (findings, prompt/output assessments, the softening result, the routing plan,
  the decision record). Pydantic v2, frozen, serialise cleanly.
* :mod:`.classifier` — the **pluggable classifier seam** (text + sampled frame),
  a Protocol with a deterministic in-repo fake so the whole gateway runs with
  **zero network and zero spend** in tests.
* :mod:`.rules` — the **deterministic rule engine**: a rule list maps spans/labels
  to categories + severity and a per-category disposition, so an obvious hit is
  decided without a model.
* :mod:`.softener` — the **intent-preserving prompt auto-softener**: rewrites
  literary violence/sexuality into tasteful framing that satisfies a provider's
  policy *rather than hard-blocking*, recording exactly what it changed.
* :mod:`.profiles` — **per-provider POLICY PROFILES**: what each model refuses, so
  the router can avoid sending content a provider will reject (fewer wasted spends).
* :mod:`.routing` — turns a prompt assessment + the profile registry into a
  **routing plan**: which providers are viable, which to avoid, and why.
* :mod:`.advisory` — the **age-rating / content-advisory tagger** for a whole book.
* :mod:`.decision_log` — the **immutable, hash-chained DECISION LOG** with
  appeal/override hooks. Pure + in-memory by default (tests), DB-backed in prod.
* :mod:`.prompt_gate` — the pre-generation gate: classify → rule engine → soften →
  decide, emitting a typed, explainable :class:`~app.safety.contracts.PromptDecision`.
* :mod:`.output_gate` — the post-generation gate: sampled-frame classification →
  ``ALLOW`` / ``QUARANTINE`` verdict.
* :mod:`.gateway` — the :class:`SafetyGateway` façade the pipeline + router call.
* :mod:`.config` — additive, env-driven gateway settings (never touches the global
  ``Settings`` schema destructively).

Every decision is **typed and explainable** — each verdict carries the findings
that drove it, the policy version that produced it, and (for a transform) the
diff between the original and softened prompt.
"""

from __future__ import annotations

from app.safety.contracts import (
    Finding,
    OutputAssessment,
    OutputVerdict,
    PromptAssessment,
    PromptDecision,
    RoutingPlan,
    SafetyAction,
    SafetyCategory,
    SafetyContext,
    Severity,
    SofteningResult,
)
from app.safety.decision_log import DecisionLog, DecisionRecord, InMemoryDecisionLog
from app.safety.gateway import SafetyGateway, build_default_gateway

__all__ = [
    "DecisionLog",
    "DecisionRecord",
    "Finding",
    "InMemoryDecisionLog",
    "OutputAssessment",
    "OutputVerdict",
    "PromptAssessment",
    "PromptDecision",
    "RoutingPlan",
    "SafetyAction",
    "SafetyCategory",
    "SafetyContext",
    "SafetyGateway",
    "Severity",
    "SofteningResult",
    "build_default_gateway",
]
