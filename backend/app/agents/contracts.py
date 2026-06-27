"""Typed request/response contracts for the agent crew (kinora.md §7, §9).

Every agent sits behind a JSON request/response schema (§7.1), so the crew is
swappable, each message is a logged/inspectable artifact, and the deterministic
policy logic (the render-mode tree §9.3, the Critic thresholds §9.5, the
conflict-arbitration policy §7.2) can be unit-tested without a network.

These are the *creative-plane* contracts. They are intentionally distinct from
:class:`app.memory.interfaces.ShotSpec` — that model is the fully-resolved,
hash-stamped spec the render queue/cache consume; the :class:`ShotSpec` here is
the Cinematographer's design output (§7.1) before it is persisted. The Adapter's
``plan_scene`` (the ``ShotPlanner`` protocol) returns the *memory* ``ShotSpec``;
everything else in this module is the design-time shape.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Shared value objects
# --------------------------------------------------------------------------- #


class SourceSpan(BaseModel):
    """Ties a beat/shot back to the exact text it depicts (§4.2).

    ``word_range`` is ``[start, end]`` in global word-index space — the key the
    source-span index sorts on to turn a scroll position into a shot in O(log n).
    """

    model_config = ConfigDict(extra="ignore")

    # Default 0 = "unknown"; the Adapter backfills the real page from the request.
    page: int = 0
    para: int | None = None
    word_range: tuple[int, int] = (0, 0)


class EstCost(BaseModel):
    """Per-shot cost estimate. ``video_seconds`` is the scarce, hard-capped unit."""

    model_config = ConfigDict(extra="forbid")

    video_seconds: float = 0.0
    tokens: int = 0


class Camera(BaseModel):
    """Camera move/speed/framing for a shot (the §7.1 ``camera`` block)."""

    model_config = ConfigDict(extra="ignore")

    move: str = "static"
    speed: str = "medium"
    shot_size: str = "medium"


class RenderMode(StrEnum):
    """The Wan 2.7 render modes selected by the §9.3 decision tree.

    Values are identical to :class:`app.providers.types.WanMode` so the Generator
    maps one to the other by value, but the agents layer stays self-contained and
    does not import the provider enum.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


# --------------------------------------------------------------------------- #
# Adapter — beats & the shot list (§4.2, §10)
# --------------------------------------------------------------------------- #


class Beat(BaseModel):
    """The smallest planning atom: a sentence-or-two of narrative intent (§4.2).

    ``entities`` are canon names the Adapter could resolve from the text; an
    entity it is unsure about is flagged ``unresolved`` (the Adapter never
    invents a character, per the §10 guardrail).
    """

    model_config = ConfigDict(extra="ignore")

    # Default "": the model emits content, the Adapter assigns the canonical id.
    beat_id: str = ""
    scene_id: str | None = None
    beat_index: int = 0
    summary: str
    entities: list[str] = Field(default_factory=list)
    unresolved_entities: list[str] = Field(default_factory=list)
    described_visuals: str | None = None
    mood: str | None = None
    source_span: SourceSpan = Field(default_factory=SourceSpan)


class ShotListItem(BaseModel):
    """One shot in the Adapter's decomposition: a ~5s clip with its source span."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    beat_id: str
    scene_id: str | None = None
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    est_duration_s: float = 5.0
    est_cost: EstCost = Field(default_factory=EstCost)


class Segment(BaseModel):
    """A packed run of consecutive beats rendered as ONE ≤15s continuous take.

    The single-clip pipeline groups consecutive same-page beats up to the wan2.7
    15s ceiling (see :mod:`app.render.segment_packer`) and the Cinematographer
    designs one continuous i2v take per segment — replacing the many-stitched-5s-
    shots structure with a single seam-free clip per moment. A scene yielding more
    than one segment is reassembled by the existing stitcher.
    """

    model_config = ConfigDict(extra="forbid")

    segment_id: str
    ordinal: int = 0
    beat_ids: list[str] = Field(default_factory=list)
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    duration_s: float = 0.0


# --------------------------------------------------------------------------- #
# Cinematographer — the shot spec (§7.1)
# --------------------------------------------------------------------------- #


class ShotSpec(BaseModel):
    """The Cinematographer's design output (§7.1).

    ``render_mode`` is chosen by the deterministic §9.3 tree; the model fills
    ``prompt``/``negative_prompt``/``camera``/``seed`` and *selects*
    ``reference_image_ids`` from the canon slice's locked refs (verbatim — never
    invented).
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    beat_id: str | None = None
    scene_id: str | None = None
    render_mode: RenderMode = RenderMode.REFERENCE_TO_VIDEO
    prompt: str = ""
    negative_prompt: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    camera: Camera = Field(default_factory=Camera)
    seed: int = 0
    target_duration_s: float = 5.0
    end_frame_ref: str | None = None


class CinematographerFill(BaseModel):
    """The Cinematographer LLM's creative fill (everything except ``render_mode``).

    Kept separate from :class:`ShotSpec` so the deterministic tree owns the mode
    and the model owns the prose/camera/seed; the agent assembles the two.
    """

    model_config = ConfigDict(extra="ignore")

    prompt: str = ""
    negative_prompt: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    camera: Camera = Field(default_factory=Camera)
    seed: int | None = None


class DirectorNote(BaseModel):
    """A Director-mode note bound to a shot/region (§5.4, §7.1)."""

    model_config = ConfigDict(extra="ignore")

    shot_id: str | None = None
    note: str
    region_png: str | None = None


# --------------------------------------------------------------------------- #
# Continuity / Showrunner — the conflict protocol (§7.2)
# --------------------------------------------------------------------------- #


class ConflictType(StrEnum):
    """The kind of disagreement raised onto the blackboard (§7.2)."""

    CANON_VIOLATION = "canon_violation"
    TIMELINE_CONTRADICTION = "timeline_contradiction"


class ConflictOption(StrEnum):
    """The fixed set of resolutions the Showrunner policy arbitrates between."""

    HONOR_CANON = "honor_canon"
    SURFACE_TO_USER = "surface_to_user"
    EVOLVE_CANON = "evolve_canon"


class ConflictOptionSpec(BaseModel):
    """One option on a :class:`ConflictObject` with its cost/precondition (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    id: ConflictOption
    action: str
    cost_video_s: float | None = None
    requires: str | None = None


class ConflictObject(BaseModel):
    """A first-class, structured conflict raised onto the blackboard (§7.2).

    Conflicts are objects, not ad-hoc prose, so they are inspectable, loggable,
    and arbitrated by a fixed policy.
    """

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    raised_by: str
    type: ConflictType = ConflictType.CANON_VIOLATION
    shot_id: str | None = None
    claim: str
    canon_fact: str | None = None
    current_beat: str | None = None
    contradicting_state_id: str | None = None
    user_facing: bool = True
    options: list[ConflictOptionSpec] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    """The Showrunner's resolution of a conflict, written to episodic memory (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    chosen_option: ConflictOption
    reasoning: str
    evolved_canon: bool = False


class ContinuityResult(BaseModel):
    """Continuity's verdict on a proposed shot: clean, or a structured conflict."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    conflict: ConflictObject | None = None


# --------------------------------------------------------------------------- #
# Critic — the QA record (§9.5)
# --------------------------------------------------------------------------- #


class Verdict(StrEnum):
    """The Critic's binary verdict (a wrong face is a fail even if it's pretty)."""

    PASS = "pass"
    FAIL = "fail"


class RepairAction(StrEnum):
    """How the Critic routes a failed clip back into the pipeline (§9.5)."""

    ACCEPT = "accept"
    REGEN_TIGHTEN_REFS = "regen_tighten_refs"
    REPROMPT_STYLE = "reprompt_style"
    REGEN_NEW_SEED = "regen_new_seed"
    RAISE_CONFLICT = "raise_conflict"
    EVOLVE_CANON = "evolve_canon"
    DEGRADE = "degrade"


class QARecord(BaseModel):
    """The Critic's scorecard for one clip, against the canon slice (§9.5).

    A verdict is ``pass`` iff all four checks hold: ``ccs >= 0.85``,
    ``style_drift <= 0.08``, ``timeline_ok`` true, ``motion_artifact <= 0.25``.
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    ccs: float
    style_drift: float
    timeline_ok: bool
    contradicting_state_id: str | None = None
    motion_artifact: float
    score: float
    verdict: Verdict
    reason: str = ""
    repair_action: RepairAction = RepairAction.ACCEPT


# --------------------------------------------------------------------------- #
# Per-agent request/response wrappers
# --------------------------------------------------------------------------- #


class AnalyzePageRequest(BaseModel):
    """Adapter input: one page's text (+ any detected illustrations) (§9.1)."""

    model_config = ConfigDict(extra="ignore")

    page: int
    page_text: str
    scene_id: str | None = None
    beat_index_start: int = 0
    detected_illustrations: list[str] = Field(default_factory=list)


class AnalyzePageResponse(BaseModel):
    """Adapter output: the beats found on a page."""

    model_config = ConfigDict(extra="ignore")

    beats: list[Beat] = Field(default_factory=list)


class PlanShotsResponse(BaseModel):
    """Adapter output: a beat list decomposed into a ~5s shot list."""

    model_config = ConfigDict(extra="forbid")

    shots: list[ShotListItem] = Field(default_factory=list)


class ScenePlanItem(BaseModel):
    """One scene in the Showrunner's high-level production plan."""

    model_config = ConfigDict(extra="ignore")

    scene_index: int
    title: str | None = None
    summary: str = ""
    page_start: int | None = None
    page_end: int | None = None
    key_entities: list[str] = Field(default_factory=list)


class ScenePlan(BaseModel):
    """The Showrunner's decomposition of a book into scenes (§7)."""

    model_config = ConfigDict(extra="ignore")

    scenes: list[ScenePlanItem] = Field(default_factory=list)


class TextualSupport(BaseModel):
    """The Showrunner's judgment of whether the source text supports a change.

    Injected in tests so the arbitration policy branches are exercised without a
    network call.
    """

    model_config = ConfigDict(extra="ignore")

    supported: bool
    reasoning: str = ""


__all__ = [
    "AnalyzePageRequest",
    "AnalyzePageResponse",
    "Beat",
    "Camera",
    "CinematographerFill",
    "ConflictObject",
    "ConflictOption",
    "ConflictOptionSpec",
    "ConflictType",
    "ContinuityResult",
    "DecisionRecord",
    "DirectorNote",
    "EstCost",
    "PlanShotsResponse",
    "QARecord",
    "RenderMode",
    "RepairAction",
    "ScenePlan",
    "ScenePlanItem",
    "ShotListItem",
    "ShotSpec",
    "SourceSpan",
    "TextualSupport",
    "Verdict",
]
