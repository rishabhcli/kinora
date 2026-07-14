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
# Adapter — deep literary comprehension (§4.2, §10)
# --------------------------------------------------------------------------- #


class NarrativePerson(StrEnum):
    """Grammatical/narratorial person of a beat's telling (POV analysis, §10).

    The Adapter's literary-comprehension pass tags each beat with the *voice*
    that narrates it so multi-POV books render from the right vantage and an
    unreliable narrator can be flagged downstream rather than taken at face value.
    """

    FIRST = "first"
    SECOND = "second"
    THIRD_LIMITED = "third_limited"
    THIRD_OMNISCIENT = "third_omniscient"
    UNKNOWN = "unknown"


class DiscourseMode(StrEnum):
    """How a beat's content reaches the reader — the surface vs. the interior.

    ``free_indirect`` is the literary middle ground (the narrator's third person
    coloured by a character's diction/feeling); the Adapter detects it so a beat
    of pure interiority is rendered as subjective imagery, not a literal action.
    """

    NARRATION = "narration"
    DIALOGUE = "dialogue"
    INTERIOR_MONOLOGUE = "interior_monologue"
    FREE_INDIRECT = "free_indirect"


class SceneTempo(StrEnum):
    """The pacing of a beat's moment — drives pacing-aware shot density (§4.2).

    Tempo is the multiplier on how many shots a beat earns: a ``scene`` (real-time
    dramatised action / dialogue) gets denser coverage than ``summary`` (narrative
    compression of long spans) so the edit breathes with the prose's own rhythm.
    """

    PAUSE = "pause"  # description / stillness — held, sparse coverage
    SCENE = "scene"  # dramatised real-time action — dense coverage
    SUMMARY = "summary"  # compressed narration of long spans — sparse
    ELLIPSIS = "ellipsis"  # a jump/gap in time — a single transition shot


class TimePosition(StrEnum):
    """Where a beat sits on the STORY timeline relative to the narration (§4.2).

    Narrative-time (the order words appear on the page) is separated from
    story-time (chronological order of events) so flashbacks/flash-forwards are
    reconstructed into the order events actually happen, while the source-span
    index still keys off narrative-time for scroll-sync.
    """

    PRESENT = "present"  # on the main narrative now-line
    FLASHBACK = "flashback"  # an earlier story moment told later
    FLASHFORWARD = "flashforward"  # a later story moment told earlier
    TIMELESS = "timeless"  # habitual / gnomic / out of sequence


class DialogueLine(BaseModel):
    """One attributed line of speech within a beat (speaker diarization, §10).

    ``speaker`` is a name the Adapter resolved from the dialogue tag or nearby
    context; it is left empty when unattributable rather than guessed (the §10
    no-invent guardrail extends to attribution).
    """

    model_config = ConfigDict(extra="ignore")

    speaker: str = ""
    quote: str
    #: True when the speaker was inferred from context, not an explicit "X said".
    inferred: bool = False


class LiteraryDevice(BaseModel):
    """A figure of speech detected in a beat and its translation to shot intent.

    The §10 pipeline turns metaphor/simile/symbolism into concrete *visual*
    intent (``visual_intent``) so the Cinematographer can stage the image the
    prose evokes — e.g. "her grief was a stone" → a literal weight/sinking motif —
    without the Adapter inventing entities the canon does not know.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str  # metaphor | simile | symbol | personification | imagery | irony
    text: str  # the source phrase the device occupies
    tenor: str = ""  # what the figure is really about
    vehicle: str = ""  # the image it is expressed through
    visual_intent: str = ""  # the concrete thing to show on screen


class StoryTime(BaseModel):
    """A beat's position on the reconstructed STORY timeline (non-linear, §4.2).

    ``order`` is the chronological rank used to render flashbacks/flash-forwards
    in story order; ``narrative_order`` is the beat's position in the text (the
    scroll-sync key). They differ exactly when the prose tells events out of
    sequence. ``marker`` keeps the verbatim cue ("years before", "that morning").
    """

    model_config = ConfigDict(extra="forbid")

    position: TimePosition = TimePosition.PRESENT
    #: Chronological rank among beats (lower = earlier in story-time).
    order: int = 0
    #: Position in the narration as read (mirrors ``Beat.beat_index``).
    narrative_order: int = 0
    #: The verbatim temporal cue the classifier keyed on, if any.
    marker: str | None = None


class Beat(BaseModel):
    """The smallest planning atom: a sentence-or-two of narrative intent (§4.2).

    ``entities`` are canon names the Adapter could resolve from the text; an
    entity it is unsure about is flagged ``unresolved`` (the Adapter never
    invents a character, per the §10 guardrail).

    The deep-comprehension fields below are all OPTIONAL and default to a neutral
    value, so a beat produced by the legacy single-pass path is still valid; the
    literary-comprehension pass (:mod:`app.agents.comprehension`) fills them.
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

    # -- deep literary comprehension (all additive, default-neutral) --------- #
    #: The narrating voice (multi-POV / unreliable-narrator handling).
    pov: NarrativePerson = NarrativePerson.UNKNOWN
    #: The point-of-view character whose vantage the beat is told from, if any.
    pov_character: str | None = None
    #: Whether this beat's telling is flagged unreliable (irony, deception, bias).
    unreliable: bool = False
    #: How the content reaches the reader (narration / dialogue / interiority).
    discourse: DiscourseMode = DiscourseMode.NARRATION
    #: Verbatim or paraphrased interior content when the beat is interiority.
    interiority: str | None = None
    #: Attributed lines of speech within the beat (speaker diarization).
    dialogue: list[DialogueLine] = Field(default_factory=list)
    #: Figures of speech translated to concrete visual intent.
    devices: list[LiteraryDevice] = Field(default_factory=list)
    #: The pacing of the moment — drives pacing-aware shot density.
    tempo: SceneTempo = SceneTempo.SCENE
    #: Position on the reconstructed story timeline (non-linear ordering).
    story_time: StoryTime = Field(default_factory=StoryTime)


class ShotIntent(BaseModel):
    """The comprehension-derived staging brief for a beat (Adapter → Cinematographer).

    A structured distillation of a beat's deep comprehension into directing
    guidance the Cinematographer conditions on: whether to stage the shot
    literally or as a SUBJECTIVE/POV image (interiority / free-indirect / an
    unreliable narrator), whose vantage to shoot from, the figures of speech to
    realise visually, the speakers in frame, and a pacing hint. It carries no
    invented entities — every name is one comprehension resolved from the text
    (the §10 guardrail). All fields are optional/neutral so an un-comprehended
    beat yields an empty (harmless) intent.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stage as the character's inner view rather than a literal exterior action.
    subjective: bool = False
    #: The POV character whose vantage the shot adopts, if any.
    pov_character: str | None = None
    #: Treat depicted facts as a biased/coloured CLAIM (unreliable narrator).
    unreliable: bool = False
    #: Concrete visual instructions translated from the beat's literary devices.
    visual_motifs: list[str] = Field(default_factory=list)
    #: Distinct named speakers present in the beat's dialogue (in order).
    speakers: list[str] = Field(default_factory=list)
    #: A short pacing hint ("held", "brisk", "single transition", …).
    pacing: str = ""
    #: A one-line natural-language brief assembled from the above.
    brief: str = ""


class ShotListItem(BaseModel):
    """One shot in the Adapter's decomposition: a ~5s clip with its source span."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    beat_id: str
    scene_id: str | None = None
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    est_duration_s: float = 5.0
    est_cost: EstCost = Field(default_factory=EstCost)
    #: Comprehension-derived staging brief for this shot's beat (may be empty).
    intent: ShotIntent = Field(default_factory=lambda: ShotIntent())


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
    """The Showrunner's resolution of a conflict, written to episodic memory (§7.2).

    ``recommended_option`` / ``scores`` are additive, optional fields populated by
    the series-scale weighed arbitration (:mod:`app.agents.series.arbitration`).
    They are *advisory*: the §7.2 hard gate still owns ``chosen_option``. Older
    callers that never set them serialize identically to before.
    """

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    chosen_option: ConflictOption
    reasoning: str
    evolved_canon: bool = False
    recommended_option: ConflictOption | None = None
    scores: dict[str, float] = Field(default_factory=dict)


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

    The fields below ``repair_action`` are an **additive** extension owned by the
    learned-reward QA subsystem (``app/render/reward.py`` + ``app/render/qa``). They
    are all optional with neutral defaults so every existing producer/consumer keeps
    working: the pre-registered four-check gate still *decides* the verdict, while
    the learned reward, per-character identity, temporal/aesthetic axes and the
    anomaly score only *inform* (e.g. ``flagged_for_review`` surfaces a gate-passing
    but low-reward / out-of-distribution clip to the director feed). See
    ``docs/design/critic-reward-model.md`` for the QA design.
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
    # -- learned-reward / multimodal QA extension (additive, §9.5/§13) -------- #
    learned_reward: float | None = None
    flagged_for_review: bool = False
    anomaly_score: float | None = None
    per_character_ccs: dict[str, float] | None = None
    temporal: float | None = None
    aesthetic: float | None = None


# --------------------------------------------------------------------------- #
# Series-scale showrunning — cross-book canon, arcs, pacing, structure (§7, §8)
#
# The single-book contracts above describe one volume. The models below let the
# Showrunner reason about a *series*: multiple volumes that share characters,
# relationships, themes and a continuity that must hold across books. Everything
# here is additive and design-time — the per-book canon graph (§8.1) and episodic
# store (§8.2) remain the source of truth for entity state; the series layer is a
# thin cross-book *index* and a set of read models computed from structured plan
# signals (no new ingest). The decisions over these models live in
# :mod:`app.agents.series` as pure functions, mirroring :func:`decide_arbitration`.
# --------------------------------------------------------------------------- #


class ArcStage(StrEnum):
    """Where a character/relationship sits in its dramatic arc (§7).

    The ordering is monotone: an arc advances forward through these stages over
    the course of a series. ``intensity`` (carried on :class:`ArcBeat`) captures
    the magnitude within a stage; the stage captures the *shape*.
    """

    SETUP = "setup"
    RISING = "rising"
    TURN = "turn"
    CLIMAX = "climax"
    FALLING = "falling"
    RESOLUTION = "resolution"


#: Canonical forward ordering of :class:`ArcStage` (index = progression rank).
ARC_STAGE_ORDER: tuple[ArcStage, ...] = (
    ArcStage.SETUP,
    ArcStage.RISING,
    ArcStage.TURN,
    ArcStage.CLIMAX,
    ArcStage.FALLING,
    ArcStage.RESOLUTION,
)


class RelationshipKind(StrEnum):
    """The character-pair relationship kinds the series layer tracks (§7)."""

    ALLY = "ally"
    RIVAL = "rival"
    FAMILY = "family"
    ROMANTIC = "romantic"
    MENTOR = "mentor"
    ENEMY = "enemy"
    NEUTRAL = "neutral"


class Volume(BaseModel):
    """One book within a series; ties a series position to a canon ``book_id``.

    The series is an ordered list of volumes. ``book_id`` is the per-book canon's
    id (§8.1) when the volume has been ingested; it is ``None`` for a volume that
    is planned but not yet imported.
    """

    model_config = ConfigDict(extra="forbid")

    volume_index: int
    title: str | None = None
    book_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    beat_count: int = 0
    synopsis: str = ""


class ArcBeat(BaseModel):
    """A single sampled point on a character's (or relationship's) arc (§7).

    ``intensity`` is a 0..1 magnitude of dramatic charge at this beat; ``stage``
    is the arc stage it belongs to. The series-position key is
    ``(volume_index, beat_index)`` so points sort across volumes the way the
    source-span index sorts within a book (§4.2).
    """

    model_config = ConfigDict(extra="ignore")

    volume_index: int = 0
    beat_index: int = 0
    stage: ArcStage = ArcStage.SETUP
    intensity: float = 0.0
    summary: str = ""
    source_span: SourceSpan = Field(default_factory=SourceSpan)


class ArcState(BaseModel):
    """The *resolved* arc-state at a series position — the read model (§8.5).

    Like a continuity state resolved "as of" a beat (§8.5 forgetting), an arc
    resolved at ``(volume, beat)`` reflects only the beats up to that point, so a
    time-travel read (the reader scrolls back) sees the arc as it stood then.
    """

    model_config = ConfigDict(extra="forbid")

    stage: ArcStage = ArcStage.SETUP
    intensity: float = 0.0
    last_volume: int = 0
    last_beat: int = 0
    beats_seen: int = 0


class CharacterArc(BaseModel):
    """A character's evolving arc across the volumes of a series (§7)."""

    model_config = ConfigDict(extra="ignore")

    entity_key: str
    name: str = ""
    beats: list[ArcBeat] = Field(default_factory=list)
    spanned_volumes: list[int] = Field(default_factory=list)


class RelationshipArc(BaseModel):
    """A relationship's trajectory between two characters across volumes (§7).

    ``entity_keys`` is the unordered pair (sorted on construction so a relation is
    keyed canonically). ``kind`` is its dominant flavour; the ``beats`` carry the
    evolving intensity (e.g. allies drifting into rivals).
    """

    model_config = ConfigDict(extra="ignore")

    entity_keys: tuple[str, str]
    kind: RelationshipKind = RelationshipKind.NEUTRAL
    beats: list[ArcBeat] = Field(default_factory=list)
    spanned_volumes: list[int] = Field(default_factory=list)


class MotifKind(StrEnum):
    """How a motif recurs at a given callback point (§7 thematic planning)."""

    PLANT = "plant"
    ECHO = "echo"
    PAYOFF = "payoff"


class Motif(BaseModel):
    """A thematic motif and where it should recur across the series (§7).

    A motif is *planted* once, *echoed* through the middle, and *paid off* near a
    climax. ``payoff_volumes`` names the volumes whose climaxes should land the
    motif; the scheduler (:mod:`app.agents.series.motifs`) turns this into
    concrete :class:`MotifCallback` points.
    """

    model_config = ConfigDict(extra="ignore")

    motif_id: str
    label: str = ""
    description: str = ""
    planted_volume: int = 0
    planted_beat: int = 0
    payoff_volumes: list[int] = Field(default_factory=list)


class MotifCallback(BaseModel):
    """A scheduled recurrence of a motif at a concrete series position (§7)."""

    model_config = ConfigDict(extra="forbid")

    motif_id: str
    kind: MotifKind
    volume_index: int
    beat_index: int
    note: str = ""


class TensionPoint(BaseModel):
    """One sample of the narrative-tension curve at a series position (§7)."""

    model_config = ConfigDict(extra="forbid")

    volume_index: int = 0
    beat_index: int = 0
    tension: float = 0.0
    source_span: SourceSpan = Field(default_factory=SourceSpan)


class MonotonyRun(BaseModel):
    """A flat stretch of the pacing curve (low tension variance) — what to fix."""

    model_config = ConfigDict(extra="forbid")

    start_index: int
    end_index: int
    mean_tension: float
    length: int


class PacingCurve(BaseModel):
    """The narrative-tension/pacing curve the planner optimizes against (§7).

    Derived stats are computed once by :func:`app.agents.series.pacing.tension_curve`
    so consumers (the planner, the structure detector, the eval harness) read them
    instead of recomputing: ``peak_index`` is the climax sample, ``mean_tension``
    the average charge, ``monotony_runs`` the flat stretches that need a re-plan.
    """

    model_config = ConfigDict(extra="forbid")

    points: list[TensionPoint] = Field(default_factory=list)
    peak_index: int = 0
    mean_tension: float = 0.0
    monotony_runs: list[MonotonyRun] = Field(default_factory=list)


class ActBoundary(BaseModel):
    """A detected act break (or midpoint) within a volume (§7).

    ``kind`` is ``act`` for a major structural break and ``midpoint`` for the
    mid-act turn; ``tension_delta`` is the sustained tension change that marked it.
    """

    model_config = ConfigDict(extra="forbid")

    volume_index: int = 0
    at_beat: int = 0
    kind: str = "act"
    tension_delta: float = 0.0


class EpisodeBoundary(BaseModel):
    """An episode (binge-unit) boundary cut from the pacing curve (§7).

    Episodes end on a local tension peak so they close on a cliffhanger; the last
    episode of a volume closes on its climax/resolution and is not a cliffhanger.
    """

    model_config = ConfigDict(extra="forbid")

    episode_index: int
    volume_index: int = 0
    beat_start: int = 0
    beat_end: int = 0
    title: str | None = None
    cliffhanger: bool = True
    peak_tension: float = 0.0


class RecapItem(BaseModel):
    """One prior beat selected for a "previously on" recap (§7)."""

    model_config = ConfigDict(extra="forbid")

    volume_index: int
    beat_index: int
    summary: str = ""
    weight: float = 0.0
    est_seconds: float = 0.0
    motif_ids: list[str] = Field(default_factory=list)


class RecapSpec(BaseModel):
    """The "previously on" plan for the start of a volume/episode (§7, §8.7).

    The items are chosen under a video-second budget (§11); a recap reuses already
    accepted clips from episodic memory (§8.2) so it costs near-zero new
    video-seconds (§8.7). The Showrunner fills the narration prose over this plan.
    """

    model_config = ConfigDict(extra="forbid")

    for_volume: int
    items: list[RecapItem] = Field(default_factory=list)
    total_target_s: float = 0.0
    narration: str = ""


class SeriesBible(BaseModel):
    """The cross-book canon index for a whole series (§7, §8.1).

    A thin index over the per-book canon: it references entity keys, never
    duplicates appearance/state. It records what no single book's canon can — the
    volume ordering, each character's and relationship's arc across volumes, and
    the thematic motifs with their planned callbacks.
    """

    model_config = ConfigDict(extra="ignore")

    series_id: str
    title: str = ""
    volumes: list[Volume] = Field(default_factory=list)
    character_arcs: list[CharacterArc] = Field(default_factory=list)
    relationship_arcs: list[RelationshipArc] = Field(default_factory=list)
    motifs: list[Motif] = Field(default_factory=list)
    synopsis: str = ""


class ArbitrationContext(BaseModel):
    """The richer signals the series-scale arbitration weighs (§7.2).

    These extend the §7.2 policy *without replacing it*: the hard gate (no evolve
    without textual support) is unchanged. When honor and surface both remain
    eligible, these decide which better serves the series — a high
    ``dramatic_stakes`` near a climax favours surfacing to the reader, a high
    ``arc_continuity_weight`` favours honoring the established arc.
    """

    model_config = ConfigDict(extra="forbid")

    arc_continuity_weight: float = 0.5
    dramatic_stakes: float = 0.5
    motif_payoff_pending: bool = False
    in_climax: bool = False
    spans_volumes: bool = False


class ArbitrationDecision(BaseModel):
    """A scored, explainable arbitration outcome (§7.2 series-scale).

    A superset of :class:`DecisionRecord`'s decision fields: ``chosen_option`` is
    still the §7.2-gated authoritative pick; ``recommended_option`` is what the
    score favoured (it may match or, for honor-vs-surface, refine it); ``scores``
    is the per-option weighing for transparency in the agent-activity feed.
    """

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    chosen_option: ConflictOption
    recommended_option: ConflictOption
    evolved_canon: bool = False
    scores: dict[str, float] = Field(default_factory=dict)
    reasoning: str = ""


class CrossVolumeConflict(BaseModel):
    """A contradiction between a proposed beat and a *prior volume*'s canon (§7.2).

    The cross-book counterpart of the per-book :class:`ConflictObject`: a Volume-3
    depiction that violates an established Volume-1 fact. ``prior_volume_index``
    cites the offending earlier volume so the Showrunner can weigh continuity.
    """

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    subject_entity_key: str
    claim: str
    prior_fact: str
    prior_volume_index: int
    current_volume_index: int
    current_beat_index: int = 0


class ArcCoherenceReport(BaseModel):
    """§13 eval: does each tracked arc advance monotonically across the series."""

    model_config = ConfigDict(extra="forbid")

    arcs_checked: int = 0
    monotonic_arcs: int = 0
    regressions: list[str] = Field(default_factory=list)
    coherence: float = 1.0


class PacingReport(BaseModel):
    """§13 eval: pacing quality of a volume's curve."""

    model_config = ConfigDict(extra="forbid")

    score: float = 0.0
    mean_tension: float = 0.0
    peak_position: float = 0.0
    monotony_fraction: float = 0.0
    longest_flat_run: int = 0


class MotifReport(BaseModel):
    """§13 eval: did every planted motif pay off across the series."""

    model_config = ConfigDict(extra="forbid")

    motifs_checked: int = 0
    paid_off: int = 0
    unresolved: list[str] = Field(default_factory=list)
    payoff_rate: float = 1.0


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
    """One scene in the Showrunner's high-level production plan.

    ``volume_index`` / ``act`` / ``tension`` are additive series-scale fields:
    which volume the scene belongs to, the act it sits in (filled by the §7
    structure detector), and its planned narrative tension (0..1). All default,
    so a single-book plan is unchanged.
    """

    model_config = ConfigDict(extra="ignore")

    scene_index: int
    title: str | None = None
    summary: str = ""
    page_start: int | None = None
    page_end: int | None = None
    key_entities: list[str] = Field(default_factory=list)
    volume_index: int = 0
    act: int | None = None
    tension: float | None = None


class ScenePlan(BaseModel):
    """The Showrunner's decomposition of a book into scenes (§7).

    ``series_id`` / ``volume_index`` / ``pacing_curve`` are additive series-scale
    fields: the series this plan belongs to, the volume's position in it, and the
    optimized pacing curve the planner produced. All default — a one-book plan
    serializes exactly as before.
    """

    model_config = ConfigDict(extra="ignore")

    scenes: list[ScenePlanItem] = Field(default_factory=list)
    series_id: str | None = None
    volume_index: int = 0
    pacing_curve: PacingCurve | None = None


class TextualSupport(BaseModel):
    """The Showrunner's judgment of whether the source text supports a change.

    Injected in tests so the arbitration policy branches are exercised without a
    network call.
    """

    model_config = ConfigDict(extra="ignore")

    supported: bool
    reasoning: str = ""


__all__ = [
    "ARC_STAGE_ORDER",
    "AnalyzePageRequest",
    "AnalyzePageResponse",
    "ArbitrationContext",
    "ArbitrationDecision",
    "ArcBeat",
    "ArcCoherenceReport",
    "ArcStage",
    "ArcState",
    "Beat",
    "Camera",
    "CharacterArc",
    "CinematographerFill",
    "ConflictObject",
    "ConflictOption",
    "ConflictOptionSpec",
    "ConflictType",
    "ContinuityResult",
    "CrossVolumeConflict",
    "DecisionRecord",
    "DialogueLine",
    "DirectorNote",
    "DiscourseMode",
    "EstCost",
    "ActBoundary",
    "EpisodeBoundary",
    "LiteraryDevice",
    "MonotonyRun",
    "Motif",
    "MotifCallback",
    "MotifKind",
    "MotifReport",
    "NarrativePerson",
    "PacingCurve",
    "PacingReport",
    "PlanShotsResponse",
    "QARecord",
    "RecapItem",
    "RecapSpec",
    "RelationshipArc",
    "RelationshipKind",
    "RenderMode",
    "RepairAction",
    "SceneTempo",
    "ScenePlan",
    "ScenePlanItem",
    "SeriesBible",
    "ShotIntent",
    "ShotListItem",
    "ShotSpec",
    "SourceSpan",
    "StoryTime",
    "TensionPoint",
    "TextualSupport",
    "TimePosition",
    "Verdict",
    "Volume",
]
