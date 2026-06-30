"""Typed models for model-agnostic prompt-to-storyboard planning (§9.1, §9.3).

A *storyboard* sits one rung above the Round-1 prompt-dialect layer: given a raw
passage (a text span plus the canon context that applies to it), the planner
produces an ordered list of :class:`StoryboardShot`s — the shot list the
Cinematographer/dialect layer renders. Each shot carries a beat reference, a
ShotDescription-shaped *intent* (the canonical, model-agnostic staging brief), a
suggested :class:`app.agents.contracts.RenderMode`, a duration, a camera block, the
entities present, a continuity hand-off to the previous shot, and the slice of
narration text it covers.

The models are deliberately decoupled from the agent crew: they reuse the
*value objects* in :mod:`app.agents.contracts` (``RenderMode``, ``Camera``,
``SourceSpan``, ``SceneTempo``) so the output drops straight into the existing
contracts without importing the agents themselves or the Round-1 dialect/planner
modules. The planner only ever *produces* this canonical shape.

Everything here is pure data: pydantic v2, deterministic, no I/O.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.contracts import Camera, RenderMode, SceneTempo, SourceSpan

# --------------------------------------------------------------------------- #
# Coverage — the shot's editorial role within a beat
# --------------------------------------------------------------------------- #


class ShotCoverage(StrEnum):
    """The editorial role a shot plays in covering a beat (classic film grammar).

    A faithful edit does not render a beat as one flat take: it *covers* the
    moment from complementary angles. The planner assigns a coverage role to each
    shot so the dialect layer can stage it correctly and the §9.3 render-mode tree
    has the establishing/insert signals it branches on.

    - ``establishing`` — sets the scene/location (wide; often no character → t2v).
    - ``master`` — the principal staging of the action (the spine of the beat).
    - ``insert`` — a detail/cutaway (a hand, an object, a prop the prose names).
    - ``reaction`` — a character's response (close on the listener, not the speaker).
    - ``pov`` — a subjective shot from a character's vantage (interiority/free-indirect).
    - ``transition`` — a bridge between beats (an ELLIPSIS jump → a single cut).
    """

    ESTABLISHING = "establishing"
    MASTER = "master"
    INSERT = "insert"
    REACTION = "reaction"
    POV = "pov"
    TRANSITION = "transition"


#: Coverage roles that depict a character in frame (never an empty establishing).
CHARACTER_COVERAGE: frozenset[ShotCoverage] = frozenset(
    {ShotCoverage.MASTER, ShotCoverage.REACTION, ShotCoverage.POV}
)


# --------------------------------------------------------------------------- #
# Input — a passage to be storyboarded
# --------------------------------------------------------------------------- #


class CanonContext(BaseModel):
    """The canon slice a passage is planned against (the model-agnostic view).

    A minimal, transport-friendly projection of the memory canon slice: the
    entity keys present, which of them have a locked reference (so the dialect
    layer can pin appearance, and the render-mode tree knows a character can be
    locked), the active location, and the style tokens. The planner never invents
    an entity outside this set — the §10 no-invent guardrail carried up a layer.
    """

    model_config = ConfigDict(extra="ignore")

    #: Entity keys present in this passage (characters + props), canonical form.
    entities: list[str] = Field(default_factory=list)
    #: Subset of ``entities`` that have a locked reference image in the canon.
    locked_entities: list[str] = Field(default_factory=list)
    #: The active location entity key, if any (drives establishing shots).
    location: str | None = None
    #: Style tokens (palette/lens/grade ids) the dialect layer conditions on.
    style_tokens: list[str] = Field(default_factory=list)

    def is_locked(self, entity: str) -> bool:
        """True when ``entity`` has a locked reference (appearance is pinned)."""
        return entity in set(self.locked_entities)


class PassageBeat(BaseModel):
    """One beat of a passage: a sentence-or-two of narrative intent (§4.2).

    The decomposition unit handed to the planner. A passage may arrive already
    segmented (the Adapter produced beats) or as a single span the planner
    segments itself (:func:`segment_passage`). ``text`` is the verbatim narration
    the beat covers; ``word_range`` is its global word-index span (the scroll-sync
    key); the remaining fields are optional comprehension hints.
    """

    model_config = ConfigDict(extra="ignore")

    beat_id: str
    text: str
    #: Global word-index span ``[start, end]`` (mirrors ``SourceSpan.word_range``).
    word_range: tuple[int, int] = (0, 0)
    page: int = 0
    #: Canon entity keys the beat depicts (a subset of the passage's context).
    entities: list[str] = Field(default_factory=list)
    #: Pacing tempo; defaults SCENE (neutral) — the engine classifies if unset.
    tempo: SceneTempo = SceneTempo.SCENE
    mood: str | None = None
    #: True when the beat is interiority/free-indirect → a POV/subjective shot.
    subjective: bool = False
    #: The vantage character for a subjective/reaction shot, if known.
    pov_character: str | None = None


class Passage(BaseModel):
    """A text span + its canon context — the planner's input (§9.1 step 4).

    Either supply ``beats`` (already segmented) or ``text`` (the engine segments
    it into beats). ``scene_id`` groups shots; ``word_offset`` is the global word
    index the passage's text begins at (so segmentation can emit absolute spans).
    """

    model_config = ConfigDict(extra="ignore")

    passage_id: str
    scene_id: str | None = None
    text: str = ""
    #: Pre-segmented beats; when empty the engine segments ``text``.
    beats: list[PassageBeat] = Field(default_factory=list)
    context: CanonContext = Field(default_factory=CanonContext)
    #: Global word index where ``text`` starts (for absolute source spans).
    word_offset: int = 0
    page: int = 0


# --------------------------------------------------------------------------- #
# Budget — pacing target + §-style shot-count ceiling
# --------------------------------------------------------------------------- #


class StoryboardBudget(BaseModel):
    """Pacing target + shot-count ceiling the planner fits the storyboard to.

    ``target_total_s`` is the screen-time the whole storyboard should sum to (the
    pacing target); ``tolerance_s`` is how far the realised total may drift before
    the validators flag it. ``max_shots`` is the §-style shot-count budget — the
    hard ceiling on how many clips a passage earns (the scarce video-seconds are
    bounded by reading, §4.4). ``min/max_shot_s`` clamp any single clip to the
    wan duration band.
    """

    model_config = ConfigDict(extra="forbid")

    target_total_s: float = 30.0
    tolerance_s: float = 2.0
    max_shots: int = 12
    min_shots: int = 1
    min_shot_s: float = 3.0
    max_shot_s: float = 8.0

    @classmethod
    def from_settings(cls, settings: object) -> StoryboardBudget:
        """Build a budget from the additive ``storyboard_*`` settings.

        Accepts any object exposing the ``storyboard_target_total_s`` family of
        attributes (the app ``Settings``); falls back to the field defaults for
        any attribute the object does not provide, so it is safe to call with a
        partial/stub settings object in tests.
        """
        def _get(name: str, default: float | int) -> float | int:
            return getattr(settings, name, default)

        return cls(
            target_total_s=float(_get("storyboard_target_total_s", 30.0)),
            tolerance_s=float(_get("storyboard_tolerance_s", 2.0)),
            max_shots=int(_get("storyboard_max_shots", 12)),
            min_shot_s=float(_get("storyboard_min_shot_s", 3.0)),
            max_shot_s=float(_get("storyboard_max_shot_s", 8.0)),
        )

    @model_validator(mode="after")
    def _check(self) -> StoryboardBudget:
        if self.target_total_s <= 0:
            raise ValueError("target_total_s must be > 0")
        if self.min_shot_s <= 0 or self.max_shot_s < self.min_shot_s:
            raise ValueError("require 0 < min_shot_s <= max_shot_s")
        if self.max_shots < self.min_shots or self.min_shots < 1:
            raise ValueError("require 1 <= min_shots <= max_shots")
        if self.tolerance_s < 0:
            raise ValueError("tolerance_s must be >= 0")
        return self


# --------------------------------------------------------------------------- #
# Continuity — the hand-off between consecutive shots
# --------------------------------------------------------------------------- #


class ContinuityKind(StrEnum):
    """The kind of cut linking two consecutive storyboard shots."""

    SCENE_START = "scene_start"
    CONTINUOUS = "continuous"
    MATCH_FRAME = "match_frame"
    HARD_CUT = "hard_cut"


class ContinuityLink(BaseModel):
    """How a shot hands off from the previous one (the cut between two clips).

    ``CONTINUOUS`` means the next shot continues from the previous *accepted*
    endpoint frame (the §9.3 ``video_continuation`` discipline): the previous
    shot's last frame is the next shot's first frame, so ``from_shot_id`` names
    the anchor. ``MATCH_FRAME`` is a first/last-frame hand-off — the next shot
    must *open* on a composition that matches the previous close (a graphic match
    cut) without literally continuing the take. ``HARD_CUT`` is an unrelated new
    setup; ``SCENE_START`` opens a storyboard/scene with no predecessor.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ContinuityKind = ContinuityKind.SCENE_START
    #: The shot whose last frame anchors this hand-off (None at a scene start).
    from_shot_id: str | None = None
    #: Whether the previous shot's endpoint is shared as this shot's first frame.
    shares_first_frame: bool = False


# --------------------------------------------------------------------------- #
# Output — a storyboard shot and the ordered storyboard
# --------------------------------------------------------------------------- #


class ShotIntentShape(BaseModel):
    """The canonical, model-agnostic staging brief (ShotDescription-shaped).

    This is the shape the Round-1 prompt-dialect layer renders per model: a
    structured directing brief that names *what to show*, not *how a given model's
    prompt syntax expresses it*. It deliberately mirrors the union of the agents'
    ``ShotIntent`` (subjective/pov/motifs/speakers) and the Cinematographer's
    creative fill (the action/description/reference ids) without importing either
    — keeping the storyboard layer a clean producer of the canonical contract.
    """

    model_config = ConfigDict(extra="forbid")

    #: One-line description of the action/image to stage (the prompt seed).
    action: str = ""
    #: Concrete visual motifs translated from the prose's figures of speech.
    visual_motifs: list[str] = Field(default_factory=list)
    #: Named speakers/characters in frame (in order).
    speakers: list[str] = Field(default_factory=list)
    #: The locked reference entity keys this shot pins appearance to.
    reference_entities: list[str] = Field(default_factory=list)
    #: Stage as a character's inner view rather than a literal exterior action.
    subjective: bool = False
    #: The POV character whose vantage the shot adopts, if any.
    pov_character: str | None = None
    mood: str | None = None


class StoryboardShot(BaseModel):
    """One shot in an ordered storyboard — the unit the dialect layer renders.

    A self-contained, model-agnostic shot spec: which beat it covers, its
    editorial coverage role, the suggested §9.3 render mode, its target screen
    duration, the camera block, the canon entities present, the continuity
    hand-off from the previous shot, the slice of narration it covers, and the
    canonical staging intent.
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    beat_id: str
    scene_id: str | None = None
    ordinal: int = 0
    coverage: ShotCoverage = ShotCoverage.MASTER
    render_mode: RenderMode = RenderMode.REFERENCE_TO_VIDEO
    duration_s: float = 5.0
    camera: Camera = Field(default_factory=Camera)
    #: Canon entity keys present in this shot (a subset of the beat's entities).
    entities: list[str] = Field(default_factory=list)
    continuity: ContinuityLink = Field(default_factory=ContinuityLink)
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    #: The verbatim narration text this shot covers (drives narration coverage).
    narration: str = ""
    intent: ShotIntentShape = Field(default_factory=ShotIntentShape)


class StoryboardWarning(BaseModel):
    """A non-fatal advisory the planner attached during refinement."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    shot_id: str | None = None


class Storyboard(BaseModel):
    """The ordered shot list for a passage — the planner's output (§9.1 step 4).

    Feeds straight into the Round-1 prompt-dialect + planner layers: a consumer
    walks ``shots`` in order, renders each ``intent`` through the model dialect,
    and honours the ``continuity`` hand-offs. ``total_duration_s`` is the realised
    screen-time; ``budget`` is the target it was fit to; ``warnings`` carry any
    advisories from refinement (e.g. a budget that could not be met exactly).
    """

    model_config = ConfigDict(extra="forbid")

    passage_id: str
    scene_id: str | None = None
    shots: list[StoryboardShot] = Field(default_factory=list)
    budget: StoryboardBudget = Field(default_factory=StoryboardBudget)
    warnings: list[StoryboardWarning] = Field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        """Realised screen-time = the sum of every shot's duration."""
        return round(sum(s.duration_s for s in self.shots), 3)

    @property
    def shot_count(self) -> int:
        return len(self.shots)


__all__ = [
    "CHARACTER_COVERAGE",
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
]
