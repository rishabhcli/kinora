"""Model-agnostic prompt-to-storyboard planning (§9.1 step 4, §9.3).

Turns a raw *passage* (a text span + the canon context that applies to it) into a
typed :class:`Storyboard` — an ordered list of :class:`StoryboardShot`s with
coverage roles, suggested §9.3 render modes, budgeted durations, camera, entities
present, continuity hand-offs, and narration slices. The storyboard is the rung
*above* the Round-1 prompt-dialect + planner layers: a consumer walks the shots in
order and renders each canonical ``intent`` through its model dialect. This package
only ever *produces* that canonical shape — it never imports the dialect layers.

Layers (each independently unit-testable, all network-free):

- :mod:`models` — the typed contract (``Passage`` → ``Storyboard``).
- :mod:`provider` — the pluggable ``ReasoningProvider`` seam (Qwen/OpenAI in prod;
  a scripted/heuristic stand-in in tests — no live calls).
- :mod:`segmentation` — deterministic passage → beats.
- :mod:`coverage` — deterministic beat → ordered coverage roles + render modes.
- :mod:`budget` — shot-count + duration allocation to fit a pacing target.
- :mod:`continuity` — last-frame → next-first-frame hand-offs.
- :mod:`validators` — orphan-entity / duration / narration-coverage checks.
- :mod:`engine` — the orchestration + a bounded re-plan/refine pass.

The default entry point is :func:`plan_storyboard` / :class:`StoryboardPlanner`.
"""

from __future__ import annotations

from .budget import (
    BeatAllocation,
    ShotDurationInput,
    allocate_durations,
    allocate_shot_counts,
)
from .continuity import link_continuity
from .coverage import entities_for, plan_coverage, render_mode_for, speakers_in_beat
from .engine import StoryboardPlanner, plan_storyboard
from .models import (
    CanonContext,
    ContinuityKind,
    ContinuityLink,
    Passage,
    PassageBeat,
    ShotCoverage,
    ShotIntentShape,
    Storyboard,
    StoryboardBudget,
    StoryboardShot,
    StoryboardWarning,
)
from .provider import (
    BeatPlan,
    HeuristicReasoningProvider,
    ReasoningPlan,
    ReasoningProvider,
    ScriptedReasoningProvider,
)
from .segmentation import DEFAULT_WORDS_PER_BEAT, segment_passage
from .validators import IssueSeverity, ValidationIssue, has_errors, validate_storyboard

__all__ = [
    # models
    "CanonContext",
    "ContinuityKind",
    "ContinuityLink",
    "Passage",
    "PassageBeat",
    "ShotCoverage",
    "ShotIntentShape",
    "Storyboard",
    "StoryboardBudget",
    "StoryboardShot",
    "StoryboardWarning",
    # provider
    "BeatPlan",
    "HeuristicReasoningProvider",
    "ReasoningPlan",
    "ReasoningProvider",
    "ScriptedReasoningProvider",
    # segmentation
    "DEFAULT_WORDS_PER_BEAT",
    "segment_passage",
    # coverage
    "entities_for",
    "plan_coverage",
    "render_mode_for",
    "speakers_in_beat",
    # budget
    "BeatAllocation",
    "ShotDurationInput",
    "allocate_durations",
    "allocate_shot_counts",
    # continuity
    "link_continuity",
    # validators
    "IssueSeverity",
    "ValidationIssue",
    "has_errors",
    "validate_storyboard",
    # engine
    "StoryboardPlanner",
    "plan_storyboard",
]
