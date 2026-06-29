"""Shrinking-friendly Hypothesis strategies for the deterministic policy core.

The design rule throughout: **generate from small, structured primitives so the
shrinker can drive a failure to a minimal counterexample.** Floats are bounded and
finite (no NaN/inf — those are separate, explicit edge tests), booleans are plain
``st.booleans()``, and the composite inputs (QA scorecards, conflicts, reading
positions, beats) are built with ``st.builds`` / ``@composite`` from those
primitives so a shrink collapses each field independently.

Two recurring tricks make the QA / zone / arbitration tests *find* bugs rather
than just pass:

* **Near-threshold emphasis.** Scoring gates flip exactly at a boundary (CCS 0.85,
  style 0.08, ETA = commit horizon). A uniform float rarely lands on the seam, so
  the QA / zone strategies *mix in* values sampled tightly around each threshold
  (just-below / exactly-at / just-above) — the region where an off-by-one ``<`` vs
  ``<=`` actually shows up.
* **Structured enums + flags.** Render-mode inputs and conflict options are drawn
  from the real enums so every branch of the §9.3 / §7.2 trees is reachable.

These are imported by the test modules in ``tests/verification/``; keeping them in
the package (not a ``conftest``) lets several suites share one definition and lets
mypy check them.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import strategies as st

from app.agents.cinematographer import RenderModeInputs
from app.agents.contracts import (
    Beat,
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    SourceSpan,
)
from app.render.continuity_reasoning.intervals import BeatInterval
from app.render.ladder import LadderAssets, LadderReason, Rung
from app.render.simulator import ConflictOutcome, QAVerdict, RenderScenario
from app.scheduler.optimizer import Candidate

# --------------------------------------------------------------------------- #
# Primitive numeric strategies (bounded, finite — no NaN/inf in the main runs)
# --------------------------------------------------------------------------- #

#: A score on the unit interval — the natural domain for CCS / artifact ratings.
unit_floats = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

#: A "near a threshold" sampler: just-below, exactly-at, and just-above ``t``.
#: This is what makes the gate tests exercise the ``<`` vs ``<=`` seam.
def near(threshold: float, *, span: float = 1e-6) -> st.SearchStrategy[float]:
    """Floats clustered tightly around ``threshold`` (the boundary region)."""
    return st.one_of(
        st.just(threshold),
        st.floats(
            min_value=threshold - span,
            max_value=threshold + span,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def around(threshold: float, *, span: float = 1e-6) -> st.SearchStrategy[float]:
    """A unit-clamped value that is *usually* uniform but often near ``threshold``."""
    return st.one_of(unit_floats, near(threshold, span=span))


# --------------------------------------------------------------------------- #
# §9.3 — RenderModeInputs (the Wan-mode decision tree)
# --------------------------------------------------------------------------- #

render_mode_inputs = st.builds(
    RenderModeInputs,
    locked_character_present=st.booleans(),
    needs_motion=st.booleans(),
    must_land_exact_pose=st.booleans(),
    prev_shot_accepted_continuous=st.booleans(),
    is_establishing_no_character=st.booleans(),
    minor_edit_on_accepted_clip=st.booleans(),
)
"""Every combination of the six §9.3 booleans — all 64 reachable in shrink space."""


# --------------------------------------------------------------------------- #
# §9.5 — QA scorecards (Critic routing)
# --------------------------------------------------------------------------- #

#: The pre-registered thresholds (kept here so the strategy can sample around them).
CCS_MIN = 0.85
STYLE_DRIFT_MAX = 0.08
MOTION_ARTIFACT_MAX = 0.25


@st.composite
def qa_scores(draw: st.DrawFn) -> tuple[float, float, bool, float]:
    """A ``(ccs, style_drift, timeline_ok, motion_artifact)`` scorecard.

    Each numeric axis is sampled *around its own threshold* so the four-way
    pass/fail gate is exercised right at every seam, not just in the bulk interior.
    """
    ccs = draw(around(CCS_MIN))
    style_drift = draw(around(STYLE_DRIFT_MAX))
    timeline_ok = draw(st.booleans())
    motion = draw(around(MOTION_ARTIFACT_MAX))
    return ccs, style_drift, timeline_ok, motion


@st.composite
def passing_qa_scores(draw: st.DrawFn) -> tuple[float, float, bool, float]:
    """A scorecard that PASSES the default gate *by construction* (no filtering).

    Sampling already-passing scores directly (rather than ``assume``-filtering a
    general scorecard, which rarely clears all four near-threshold gates at once)
    keeps the pass-side monotonicity properties fast and health-check clean.
    """
    ccs = draw(st.floats(min_value=CCS_MIN, max_value=1.0, allow_nan=False))
    style_drift = draw(st.floats(min_value=0.0, max_value=STYLE_DRIFT_MAX, allow_nan=False))
    motion = draw(st.floats(min_value=0.0, max_value=MOTION_ARTIFACT_MAX, allow_nan=False))
    return ccs, style_drift, True, motion


@st.composite
def failing_qa_scores(draw: st.DrawFn) -> tuple[float, float, bool, float]:
    """A scorecard that FAILS the default gate by construction (at least one axis bad)."""
    scores = draw(qa_scores())
    ccs, drift, tl, motion = scores
    passes = (
        ccs >= CCS_MIN and drift <= STYLE_DRIFT_MAX and tl and motion <= MOTION_ARTIFACT_MAX
    )
    if passes:
        # Push one axis (the timeline boolean) over the edge to guarantee a fail.
        return ccs, drift, False, motion
    return scores


# --------------------------------------------------------------------------- #
# §7.2 — Conflict objects (Showrunner arbitration)
# --------------------------------------------------------------------------- #

conflict_options = st.sampled_from(list(ConflictOption))


@st.composite
def conflict_objects(draw: st.DrawFn) -> ConflictObject:
    """A structured §7.2 conflict with an arbitrary subset of option specs.

    The set of offered options is what the arbitration gate branches on (it only
    evolves when ``EVOLVE_CANON`` is offered), so we draw a non-empty-or-empty
    subset of the three options to cover the gate's preconditions.
    """
    offered = draw(
        st.lists(conflict_options, min_size=0, max_size=3, unique=True)
    )
    options = [ConflictOptionSpec(id=opt, action=opt.value) for opt in offered]
    return ConflictObject(
        conflict_id=draw(st.text(min_size=1, max_size=8)),
        raised_by="critic",
        claim=draw(st.text(min_size=0, max_size=16)),
        user_facing=draw(st.booleans()),
        options=options,
    )


conflict_outcomes = st.builds(
    ConflictOutcome,
    action=st.sampled_from(["honor", "evolve", "accept", "surface"]),
)


# --------------------------------------------------------------------------- #
# §4.3/§4.4 — reading positions, velocities, zone inputs
# --------------------------------------------------------------------------- #

#: A focus word index (global word-index space; never negative).
focus_words = st.integers(min_value=0, max_value=2_000_000)

#: A shot's start word index.
word_index_starts = st.integers(min_value=0, max_value=2_000_000)

#: A *raw* (pre-clamp) velocity estimate in words/sec — can be 0, huge, negative
#: (backward), and is deliberately allowed past the clamp band to exercise skim.
raw_velocities = st.floats(
    min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False
)

#: A positive velocity for ETA math (the clamp floor guards div-by-zero anyway).
positive_velocities = st.floats(
    min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False
)

#: Horizon constants — kept ordered (commit < spec) as the real config guarantees.
@st.composite
def horizons(draw: st.DrawFn) -> tuple[float, float]:
    """An ordered ``(commit_horizon_s, spec_horizon_s)`` with ``commit <= spec``."""
    commit = draw(st.floats(min_value=1.0, max_value=120.0, allow_nan=False))
    spec = draw(st.floats(min_value=commit, max_value=600.0, allow_nan=False))
    return commit, spec


@dataclass(slots=True)
class FakeStability:
    """A minimal stand-in for the slice :func:`trajectory_is_stable` reads.

    Deliberately *not* frozen: the ``_StabilityState`` protocol the function reads
    declares settable attributes, so a frozen dataclass fails the structural-typing
    check. A mutable dataclass matches the protocol exactly.
    """

    raw_velocity_wps: float
    oscillating: bool


stability_states = st.builds(
    FakeStability,
    raw_velocity_wps=raw_velocities,
    oscillating=st.booleans(),
)


# --------------------------------------------------------------------------- #
# §4.2 — beats (for the segment packer + timeline reconstruction)
# --------------------------------------------------------------------------- #

@st.composite
def source_spans(draw: st.DrawFn, *, max_page: int = 6) -> SourceSpan:
    """A SourceSpan whose ``word_range`` is a valid, ordered ``[start, end]``."""
    start = draw(st.integers(min_value=0, max_value=100_000))
    length = draw(st.integers(min_value=0, max_value=500))
    page = draw(st.integers(min_value=0, max_value=max_page))
    return SourceSpan(page=page, para=0, word_range=(start, start + length))


@st.composite
def beats(draw: st.DrawFn, *, index: int = 0) -> Beat:
    """A minimal Beat sufficient for packing + duration math (no comprehension)."""
    span = draw(source_spans())
    return Beat(
        beat_id=f"beat_{draw(st.integers(min_value=0, max_value=9999)):04d}",
        beat_index=index,
        summary=draw(st.text(min_size=0, max_size=24)),
        source_span=span,
    )


@st.composite
def beat_runs(draw: st.DrawFn, *, min_size: int = 0, max_size: int = 10) -> list[Beat]:
    """A run of beats in reading order (``beat_index`` ascending, ids unique)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    out: list[Beat] = []
    for i in range(n):
        out.append(draw(beats(index=i)))
    # Make ids unique so packer/timeline keys never collide under shrinking.
    for i, b in enumerate(out):
        out[i] = b.model_copy(update={"beat_id": f"beat_{i:04d}"})
    return out


#: Per-beat duration estimates the packer is fed (the injected estimator's output).
beat_durations = st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False)


# --------------------------------------------------------------------------- #
# §8.5 — beat intervals (Allen interval algebra)
# --------------------------------------------------------------------------- #

#: Small beat coordinates so the shrinker reaches minimal interval counterexamples
#: and the ground-truth beat-set cross-check stays cheap.
beat_coords = st.integers(min_value=-5, max_value=12)


@st.composite
def beat_intervals(draw: st.DrawFn) -> BeatInterval:
    """A valid ``BeatInterval``: ``end`` is ``None`` (open) or ``>= start``.

    Includes the degenerate ``start == end`` empty interval — the half-open edge
    where "shares a beat" and "shares interior" diverge — so those cases are tested.
    """
    start = draw(beat_coords)
    end = draw(st.one_of(st.none(), st.integers(min_value=start, max_value=15)))
    return BeatInterval(start=start, end=end)


# --------------------------------------------------------------------------- #
# §4.6/§11.1 — promotion candidates (the budget knapsack)
# --------------------------------------------------------------------------- #

#: Durations on the 0.5s quantisation grid so the DP and the brute-force oracle
#: agree exactly (the optimiser ceil-quantises weights, so grid-aligned weights
#: keep the optimality cross-check honest).
_grid_durations = st.sampled_from([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0])


@st.composite
def candidates(draw: st.DrawFn) -> Candidate:
    """One promotion candidate with a grid-aligned duration + non-negative eta/dwell."""
    return Candidate(
        shot_id=f"shot_{draw(st.integers(min_value=0, max_value=9999)):04d}",
        est_duration_s=draw(_grid_durations),
        eta_s=draw(st.floats(min_value=0.0, max_value=300.0, allow_nan=False)),
        dwell_s=draw(st.floats(min_value=0.0, max_value=60.0, allow_nan=False)),
    )


@st.composite
def candidate_runs(draw: st.DrawFn, *, max_size: int = 8) -> list[Candidate]:
    """A small set of candidates with unique ids (so a chosen subset is identifiable)."""
    cands = draw(st.lists(candidates(), min_size=0, max_size=max_size))
    return [
        Candidate(
            shot_id=f"shot_{i:04d}",
            est_duration_s=c.est_duration_s,
            eta_s=c.eta_s,
            dwell_s=c.dwell_s,
        )
        for i, c in enumerate(cands)
    ]


# --------------------------------------------------------------------------- #
# §9.7 / §12.4 — ladder assets, reasons, and render scenarios (simulator input)
# --------------------------------------------------------------------------- #

ladder_assets = st.builds(
    LadderAssets,
    live_feasible=st.booleans(),
    has_keyframe=st.booleans(),
    has_locked_ref=st.booleans(),
    has_prev_endpoint=st.booleans(),
    can_image_gen=st.booleans(),
    has_page_illustration=st.booleans(),
    has_narration_audio=st.booleans(),
)

ladder_reasons = st.sampled_from(list(LadderReason))
rungs = st.sampled_from(list(Rung))


#: A QAVerdict drawn around the thresholds (so the simulator hits every route).
qa_verdicts = st.builds(
    QAVerdict,
    ccs=around(CCS_MIN),
    style_drift=around(STYLE_DRIFT_MAX),
    timeline_ok=st.booleans(),
    motion=around(MOTION_ARTIFACT_MAX),
    textual_evolution_supported=st.booleans(),
)


@st.composite
def render_scenarios(draw: st.DrawFn) -> RenderScenario:
    """A fully-scripted §9.7 scenario for the zero-IO render simulator.

    Covers the whole space the live loop walks: the live/budget gate, a QA verdict
    *sequence* (so multi-attempt repair loops run), conflict resolution, scripted
    hard-crash attempts (the poison path), and the pre-quarantine entry.
    """
    qa_sequence = draw(st.lists(qa_verdicts, min_size=1, max_size=4))
    n = len(qa_sequence)
    # Crash indices are kept inside the attempt budget so they are reachable.
    crashes = draw(
        st.frozensets(st.integers(min_value=0, max_value=max(n - 1, 0)), max_size=n)
    )
    return RenderScenario(
        shot_id="shot_prop",
        book_id="book_prop",
        live_feasible=draw(st.booleans()),
        budget_low=draw(st.booleans()),
        assets=draw(ladder_assets),
        qa_sequence=qa_sequence,
        conflict=draw(conflict_outcomes),
        target_duration_s=draw(st.floats(min_value=1.0, max_value=15.0, allow_nan=False)),
        raise_on_attempt=crashes,
        already_poisoned=draw(st.booleans()),
    )


__all__ = [
    "CCS_MIN",
    "MOTION_ARTIFACT_MAX",
    "STYLE_DRIFT_MAX",
    "FakeStability",
    "around",
    "beat_coords",
    "beat_durations",
    "beat_intervals",
    "beat_runs",
    "beats",
    "candidate_runs",
    "candidates",
    "conflict_objects",
    "conflict_outcomes",
    "conflict_options",
    "focus_words",
    "horizons",
    "ladder_assets",
    "ladder_reasons",
    "failing_qa_scores",
    "near",
    "passing_qa_scores",
    "positive_velocities",
    "qa_scores",
    "qa_verdicts",
    "raw_velocities",
    "render_mode_inputs",
    "render_scenarios",
    "rungs",
    "source_spans",
    "stability_states",
    "unit_floats",
    "word_index_starts",
]
