"""The degradation ladder as first-class, *deterministic* lanes (§4.4, §12.4).

The §9.7 pipeline already steps *down* the ladder when the live gate is off,
budget is low, or retries are exhausted (``pipeline._degrade`` /
``pipeline._select_keyframe``). That logic interleaves "which rung is reachable"
with "render the mp4". This module factors out the **selection brain** as a pure
function of the available assets + the pressure reason, so:

* the live pipeline, the simulator (:mod:`app.render.simulator`), and any future
  backfill share one rung-selection policy — they can never drift;
* the rung a shot *would* fall to is computable with **no ffmpeg, DB, or
  network** (the simulator and the demo "what-if" panel rely on this);
* every rung is a named lane with its required inputs and a relative cost class,
  so the §12.5 telemetry can attribute "why this rung" precisely.

The ladder is the one in §4.4/§12.4::

    full Wan video
      → Ken-Burns over a generated/locked keyframe still
      → Ken-Burns over the book's own page illustration
      → audio + (client-highlighted) text card

with a fifth, *top* rung — ``FULL_WAN`` — so the planner can express "the live
path is still feasible" uniformly. The selection here mirrors
``pipeline._select_keyframe`` exactly (keyframe → locked ref → prev endpoint →
image-gen all map to the *keyframe* rung; the page illustration is the rung
below; nothing left is the audio card) so swapping the pipeline onto this brain
is behaviour-preserving.

This module is intentionally free of the agents/memory/ffmpeg layers: an asset
inventory is a plain set of flags, so callers translate their world into
:class:`LadderAssets` and read back a :class:`LadderPlan`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from app.core.logging import get_logger
from app.render.degrade import DegradeRung

logger = get_logger("app.render.ladder")


class Rung(StrEnum):
    """A ladder lane, top (richest) to bottom (cheapest).

    The bottom three mirror :class:`app.render.degrade.DegradeRung` one-to-one;
    :func:`to_degrade_rung` projects onto it. ``FULL_WAN`` is the top lane (the
    live path) and has no ``DegradeRung`` because it is not a degradation.
    """

    FULL_WAN = "full_wan"
    KEN_BURNS_KEYFRAME = "ken_burns_keyframe"
    KEN_BURNS_ILLUSTRATION = "ken_burns_illustration"
    AUDIO_TEXT_ONLY = "audio_text_only"


#: Ordered, richest → cheapest. Index = a deterministic rung *rank* (0 = top).
LADDER: tuple[Rung, ...] = (
    Rung.FULL_WAN,
    Rung.KEN_BURNS_KEYFRAME,
    Rung.KEN_BURNS_ILLUSTRATION,
    Rung.AUDIO_TEXT_ONLY,
)

#: The degradation rungs only (FULL_WAN excluded) — the §12.4 ladder proper.
DEGRADE_RUNGS: tuple[Rung, ...] = LADDER[1:]

_TO_DEGRADE: dict[Rung, DegradeRung] = {
    Rung.KEN_BURNS_KEYFRAME: DegradeRung.KEN_BURNS_KEYFRAME,
    Rung.KEN_BURNS_ILLUSTRATION: DegradeRung.KEN_BURNS_ILLUSTRATION,
    Rung.AUDIO_TEXT_ONLY: DegradeRung.AUDIO_TEXT_ONLY,
}
_FROM_DEGRADE: dict[DegradeRung, Rung] = {v: k for k, v in _TO_DEGRADE.items()}


def to_degrade_rung(rung: Rung) -> DegradeRung:
    """Project a ladder :class:`Rung` onto the ffmpeg :class:`DegradeRung`.

    Raises:
        ValueError: for ``FULL_WAN`` (it is the live path, not a degradation).
    """
    try:
        return _TO_DEGRADE[rung]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{rung} is not a degradation rung") from exc


def from_degrade_rung(rung: DegradeRung) -> Rung:
    """Lift an ffmpeg :class:`DegradeRung` back into a ladder :class:`Rung`."""
    return _FROM_DEGRADE[rung]


def rank(rung: Rung) -> int:
    """The rung's index in :data:`LADDER` (0 = full Wan, 3 = audio card)."""
    return LADDER.index(rung)


class CostClass(IntEnum):
    """A *relative* cost class per lane (not seconds — an ordinal for telemetry).

    ``VIDEO_SECONDS`` is the only lane that draws down the scarce Wan budget; the
    Ken-Burns lanes are local ffmpeg CPU; the audio card is the cheapest of all.
    Ordering matches :data:`LADDER`, so ``CostClass`` falls monotonically as the
    planner steps down — a property the tests assert.
    """

    VIDEO_SECONDS = 3  # full Wan — spends the budget
    LOCAL_RENDER = 2  # Ken-Burns over a keyframe still (CPU ffmpeg)
    LOCAL_REUSE = 1  # Ken-Burns over the book's own page illustration
    AUDIO_ONLY = 0  # a narrated card — zero generation


_COST_BY_RUNG: dict[Rung, CostClass] = {
    Rung.FULL_WAN: CostClass.VIDEO_SECONDS,
    Rung.KEN_BURNS_KEYFRAME: CostClass.LOCAL_RENDER,
    Rung.KEN_BURNS_ILLUSTRATION: CostClass.LOCAL_REUSE,
    Rung.AUDIO_TEXT_ONLY: CostClass.AUDIO_ONLY,
}


def cost_class(rung: Rung) -> CostClass:
    """The relative :class:`CostClass` of a rung."""
    return _COST_BY_RUNG[rung]


@dataclass(frozen=True, slots=True)
class LadderAssets:
    """What's available to build a clip *right now* — the planner's only input.

    These flags mirror exactly the checks ``pipeline._select_keyframe`` makes, in
    the same priority order, so translating the pipeline's world into this struct
    and reading back :func:`plan_ladder` reproduces its rung choice. A caller that
    cannot cheaply know a flag should pass ``False`` (conservative: a lower rung).

    Attributes:
        live_feasible: the live Wan path is allowed (gate on *and* budget ok).
        has_keyframe: a generated/speculative keyframe still exists for the beat.
        has_locked_ref: a locked character/location reference image is present.
        has_prev_endpoint: the previous shot's accepted last-frame is present.
        can_image_gen: an image generator is wired (can synthesise a still).
        has_page_illustration: the book's own page image is present.
        has_narration_audio: TTS produced (or can produce) narration audio.
    """

    live_feasible: bool = False
    has_keyframe: bool = False
    has_locked_ref: bool = False
    has_prev_endpoint: bool = False
    can_image_gen: bool = False
    has_page_illustration: bool = False
    has_narration_audio: bool = True

    @property
    def has_any_still(self) -> bool:
        """Any source for a Ken-Burns *keyframe-rung* still (mirrors the pipeline)."""
        return (
            self.has_keyframe
            or self.has_locked_ref
            or self.has_prev_endpoint
            or self.can_image_gen
        )


#: Why the planner was asked — carried onto the plan for telemetry/defects. These
#: mirror the ``reason`` strings ``pipeline._degrade`` already logs, plus the
#: "live path is fine" sentinel.
class LadderReason(StrEnum):
    """The pressure that triggered a (re)plan (matches pipeline defect reasons)."""

    LIVE_OK = "live_ok"
    LIVE_VIDEO_DISABLED = "live_video_disabled"
    BUDGET_LOW = "budget_low"
    BUDGET_EXCEEDED = "budget_exceeded"
    RETRIES_EXHAUSTED = "retries_exhausted"
    PROVIDER_ERROR = "provider_error"
    NO_CONFLICT_RESOLVER = "no_conflict_resolver"
    POISONED = "poisoned"


#: Reasons that forbid the live lane outright (so ``FULL_WAN`` is never selected
#: even when assets allow it). ``LIVE_OK`` is the only reason that keeps it.
_FORCES_DEGRADE: frozenset[LadderReason] = frozenset(
    r for r in LadderReason if r is not LadderReason.LIVE_OK
)


@dataclass(frozen=True, slots=True)
class LaneFeasibility:
    """One lane's feasibility verdict (for an explainable plan + telemetry)."""

    rung: Rung
    feasible: bool
    cost: CostClass
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LadderPlan:
    """A deterministic rung selection + its full fallback chain (§4.4/§12.4).

    Attributes:
        selected: the highest feasible rung given assets + reason.
        reason: the pressure that triggered the plan.
        chain: the ordered feasible rungs at/below ``selected`` — the fallbacks
            the executor would walk if ``selected`` itself fails to render.
        lanes: per-lane feasibility (all four), for an explainable telemetry view.
    """

    selected: Rung
    reason: LadderReason
    chain: tuple[Rung, ...]
    lanes: tuple[LaneFeasibility, ...]

    @property
    def is_live(self) -> bool:
        """True when the live Wan path was selected (no degradation)."""
        return self.selected is Rung.FULL_WAN

    @property
    def is_degraded(self) -> bool:
        """True when a degradation rung was selected."""
        return not self.is_live

    @property
    def degrade_rung(self) -> DegradeRung | None:
        """The :class:`DegradeRung` for ``selected``, or ``None`` if live."""
        return None if self.is_live else to_degrade_rung(self.selected)

    @property
    def fallback(self) -> Rung | None:
        """The next rung below ``selected`` in the chain, or ``None`` at bottom."""
        idx = self.chain.index(self.selected)
        return self.chain[idx + 1] if idx + 1 < len(self.chain) else None

    def lane(self, rung: Rung) -> LaneFeasibility:
        """The feasibility record for a specific lane."""
        for entry in self.lanes:
            if entry.rung is rung:
                return entry
        raise KeyError(rung)


def _lane_feasibility(assets: LadderAssets, *, live_allowed: bool) -> list[LaneFeasibility]:
    """Compute each lane's feasibility (richest → cheapest), with missing inputs.

    The keyframe lane is feasible iff *any* still source exists (the pipeline's
    four-way fallback); the illustration lane needs the page image; the audio card
    is always feasible (the floor — the film never hard-stops).
    """
    keyframe_missing: tuple[str, ...] = ()
    if not assets.has_any_still:
        keyframe_missing = ("keyframe", "locked_ref", "prev_endpoint", "image_gen")
    return [
        LaneFeasibility(
            Rung.FULL_WAN,
            feasible=live_allowed and assets.live_feasible,
            cost=CostClass.VIDEO_SECONDS,
            missing=() if assets.live_feasible else ("live_feasible",),
        ),
        LaneFeasibility(
            Rung.KEN_BURNS_KEYFRAME,
            feasible=assets.has_any_still,
            cost=CostClass.LOCAL_RENDER,
            missing=keyframe_missing,
        ),
        LaneFeasibility(
            Rung.KEN_BURNS_ILLUSTRATION,
            feasible=assets.has_page_illustration,
            cost=CostClass.LOCAL_REUSE,
            missing=() if assets.has_page_illustration else ("page_illustration",),
        ),
        # The audio card is the floor — always feasible; audio is optional (a TTS
        # outage yields a *silent* card, never a crash — see pipeline._degrade).
        LaneFeasibility(Rung.AUDIO_TEXT_ONLY, feasible=True, cost=CostClass.AUDIO_ONLY),
    ]


def plan_ladder(assets: LadderAssets, reason: LadderReason = LadderReason.LIVE_OK) -> LadderPlan:
    """Select the highest feasible rung + its fallback chain (pure, deterministic).

    When ``reason`` forbids the live lane (anything but ``LIVE_OK``), ``FULL_WAN``
    is dropped even if ``assets.live_feasible`` — so a budget/gate pressure always
    degrades. The selection then walks the §12.4 ladder top-down and picks the
    first feasible lane; the ``chain`` is that lane and every feasible lane below
    it (the executor's fallback order).
    """
    live_allowed = reason not in _FORCES_DEGRADE
    lanes = _lane_feasibility(assets, live_allowed=live_allowed)
    feasible = [entry.rung for entry in lanes if entry.feasible]
    # AUDIO_TEXT_ONLY is always feasible, so ``feasible`` is never empty.
    selected = feasible[0]
    # The fallback chain is every feasible rung at/below the selection.
    sel_rank = rank(selected)
    chain = tuple(r for r in feasible if rank(r) >= sel_rank)
    plan = LadderPlan(selected=selected, reason=reason, chain=chain, lanes=tuple(lanes))
    logger.info(
        "ladder.plan",
        selected=selected.value,
        reason=reason.value,
        chain=[r.value for r in chain],
        live_allowed=live_allowed,
    )
    return plan


def degrade_chain(assets: LadderAssets, reason: LadderReason) -> tuple[Rung, ...]:
    """The fallback chain the executor walks for a *degradation* (no FULL_WAN).

    A convenience for the pipeline's degrade lane: forces a degrade reason if a
    live one was passed, so the chain always starts at a Ken-Burns/audio rung.
    """
    if reason is LadderReason.LIVE_OK:
        reason = LadderReason.RETRIES_EXHAUSTED
    return plan_ladder(assets, reason).chain


@dataclass(slots=True)
class LadderStats:
    """A running tally of rung selections — the §12.4 ladder distribution.

    Lets a session/book report how often it shipped full video vs each
    degradation rung (the "graceful degradation" proof for the demo panel).
    """

    counts: dict[Rung, int] = field(default_factory=lambda: dict.fromkeys(LADDER, 0))

    def record(self, rung: Rung) -> None:
        """Tally one shipped rung."""
        self.counts[rung] = self.counts.get(rung, 0) + 1

    def record_plan(self, plan: LadderPlan) -> None:
        """Tally a plan's selected rung."""
        self.record(plan.selected)

    @property
    def total(self) -> int:
        """Total shots tallied."""
        return sum(self.counts.values())

    @property
    def live_fraction(self) -> float:
        """Fraction shipped at the full-Wan lane (1.0 when all live, 0.0 if none)."""
        total = self.total
        return self.counts.get(Rung.FULL_WAN, 0) / total if total else 0.0

    def fraction(self, rung: Rung) -> float:
        """Fraction of shots shipped at a given rung."""
        total = self.total
        return self.counts.get(rung, 0) / total if total else 0.0

    def as_dict(self) -> dict[str, int]:
        """JSON-friendly {rung_value: count} for telemetry/the demo panel."""
        return {rung.value: count for rung, count in self.counts.items()}

    def merge(self, others: Iterable[LadderStats]) -> LadderStats:
        """Sum this tally with others (e.g. aggregate per-session into per-book)."""
        out = LadderStats(counts=dict(self.counts))
        for other in others:
            for rung, count in other.counts.items():
                out.counts[rung] = out.counts.get(rung, 0) + count
        return out


__all__ = [
    "DEGRADE_RUNGS",
    "LADDER",
    "CostClass",
    "LadderAssets",
    "LadderPlan",
    "LadderReason",
    "LadderStats",
    "LaneFeasibility",
    "Rung",
    "cost_class",
    "degrade_chain",
    "from_degrade_rung",
    "plan_ladder",
    "rank",
    "to_degrade_rung",
]
