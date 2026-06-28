"""The cinematic-language model — deterministic film grammar (§9.3, §7.1, §10).

This is the Cinematographer's *creative reasoning made into code*. Where
:mod:`app.render.shot_grammar` owns the event-level rhythm (establishing →
medium → insert) and the 180° axis, this module owns the **language of the
image**: which lens, which lighting key, which colour grade, how the camera is
blocked, and which director's eye the film is shot through.

Everything here is a **pure deterministic function** of text the Adapter already
produced (a beat's ``summary``/``mood``, a scene's beats, the canon's style
tokens). Nothing here invents a reference id, calls a model, or touches the
network — exactly the §9.3 contract that the render-mode tree already honours, so
the Cinematographer agent, the Event Director, and the Continuity QA can all read
one source of truth and the grammar is exhaustively unit-testable.

The pieces, each a layer of cinematic language:

* **Genre + mood inference** (:func:`infer_genre`, :func:`infer_mood`) — read the
  text once into a small vocabulary the rest of the model branches on.
* **Director-style emulation profiles** (:data:`STYLE_PROFILES`,
  :func:`select_style_profile`) — anamorphic-symmetry, naturalistic-handheld,
  noir-chiaroscuro, … each a coherent bundle of lens/lighting/grade/camera bias,
  chosen from genre+mood so a long adaptation has *one* directorial eye.
* **Lens / lighting / colour-grade** (:func:`lens_for`, :func:`lighting_for`,
  :func:`color_grade_for`) — derived from the profile, then nudged by the beat's
  own mood (a tender beat softens the key even in a noir film).
* **Scene coverage** (:func:`plan_coverage`) — the master / medium / CU set a
  scene is shot in, so the cut sequence is *planned* not improvised.
* **Shot / reverse-shot + eyeline match** (:func:`shot_reverse_shot`) — a
  two-hander is cut as alternating over-the-shoulder singles whose eyelines
  match across the action line.
* **Character blocking** (:func:`block_subjects`) — where the subject sits in
  frame (left/centre/right thirds) consistent with the resolved screen
  direction, so the composition reads.
* **Visual rhythm / cadence** (:func:`shot_length_cadence`) — the shot-length
  pattern an event plays, tightening as it builds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.agents.contracts import Beat, Camera
from app.render.shot_grammar import (
    AxisViolation,
    ScreenDirection,
    detect_axis_violations,
    resolve_screen_directions,
    shot_size_for,
    shot_sizes_for_event,
)

# --------------------------------------------------------------------------- #
# Genre + mood — the small vocabulary the model branches on
# --------------------------------------------------------------------------- #


class Genre(StrEnum):
    """The coarse genre a scene reads as (drives the default directorial eye)."""

    ACTION = "action"
    DRAMA = "drama"
    HORROR = "horror"
    ROMANCE = "romance"
    FANTASY = "fantasy"
    NOIR = "noir"
    NEUTRAL = "neutral"


class Mood(StrEnum):
    """The emotional register of a single beat (nudges the scene's defaults)."""

    TENSE = "tense"
    TENDER = "tender"
    SOMBER = "somber"
    TRIUMPHANT = "triumphant"
    EERIE = "eerie"
    CALM = "calm"
    NEUTRAL = "neutral"


#: Genre lexicon — ordered so the most specific genres win when cues overlap.
_GENRE_CUES: tuple[tuple[Genre, tuple[str, ...]], ...] = (
    (
        Genre.NOIR,
        ("noir", "detective", "smoke", "rain-slick", "venetian", "femme", "shadows", "cigarette"),
    ),
    (
        Genre.HORROR,
        ("horror", "dread", "monster", "blood", "scream", "haunt", "corpse", "terror", "demon"),
    ),
    (
        Genre.ACTION,
        ("chase", "sprint", "fight", "explosion", "battle", "gun", "sword", "race", "escape"),
    ),
    (
        Genre.FANTASY,
        ("dragon", "magic", "spell", "kingdom", "enchant", "wizard", "elf", "myth", "prophecy"),
    ),
    (
        Genre.ROMANCE,
        ("kiss", "love", "embrace", "tender", "longing", "lovers", "caress", "yearning"),
    ),
    (
        Genre.DRAMA,
        ("grief", "argue", "confession", "decision", "quiet", "family", "loss", "memory"),
    ),
)

#: Mood lexicon (a beat-level register, independent of the scene's genre).
_MOOD_CUES: tuple[tuple[Mood, tuple[str, ...]], ...] = (
    (Mood.TENSE, ("tense", "fear", "panic", "urgent", "danger", "threat", "frantic", "dread")),
    (Mood.EERIE, ("eerie", "uncanny", "ghostly", "haunt", "unease", "wrong", "still as death")),
    (Mood.TENDER, ("tender", "gentle", "soft", "loving", "warm", "intimate", "caress")),
    (Mood.TRIUMPHANT, ("triumph", "victory", "soar", "exult", "glorious", "rises", "rejoice")),
    (Mood.SOMBER, ("somber", "grief", "sorrow", "mourning", "loss", "weary", "melancholy")),
    (Mood.CALM, ("calm", "quiet", "peaceful", "serene", "still", "restful", "gentle morning")),
)


def _beat_text(beat: Beat) -> str:
    return f"{beat.summary or ''} {beat.described_visuals or ''} {beat.mood or ''}".lower()


def infer_genre(beats: Sequence[Beat]) -> Genre:
    """The dominant genre across a scene's beats (``NEUTRAL`` if no cue fires).

    Counts cue hits per genre over the whole scene and returns the strongest; the
    lexicon order breaks ties toward the more specific genre (noir over drama).
    """
    text = " ".join(_beat_text(b) for b in beats)
    best: Genre = Genre.NEUTRAL
    best_score = 0
    for genre, cues in _GENRE_CUES:
        score = sum(text.count(cue) for cue in cues)
        if score > best_score:
            best, best_score = genre, score
    return best


def infer_mood(beat: Beat) -> Mood:
    """The emotional register a single beat reads as (``NEUTRAL`` if no cue)."""
    text = _beat_text(beat)
    for mood, cues in _MOOD_CUES:
        if any(cue in text for cue in cues):
            return mood
    return Mood.NEUTRAL


# --------------------------------------------------------------------------- #
# Director-style emulation profiles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StyleProfile:
    """A coherent directorial eye: the bundle a whole adaptation is shot through.

    A profile is *not* a per-shot whim — it is the constant look (§8.6 ethos: the
    palette/lens are a constant across the film). Each field is a concrete prompt
    fragment so :func:`style_prompt_fragment` can render the profile straight into
    the Cinematographer's prompt.
    """

    name: str
    #: The default lens character (focal feel + depth of field).
    lens: str
    #: The default lighting key (how the scene is lit).
    lighting: str
    #: The default colour grade (the palette the colourist would pull toward).
    grade: str
    #: The camera-operating temperament — locked & composed vs. loose & handheld.
    camera_bias: str
    #: Whether the profile composes for symmetry (centred) over rule-of-thirds.
    symmetric: bool = False
    #: A one-line description for logs / the "Your directing style" surfaces.
    note: str = ""


#: The emulation library. Names are evocative-but-generic (we describe a *look*,
#: not a person) so the profile is a reusable directorial vocabulary.
STYLE_PROFILES: dict[str, StyleProfile] = {
    "anamorphic_symmetry": StyleProfile(
        name="anamorphic_symmetry",
        lens="anamorphic 40mm, shallow depth of field with oval bokeh and horizontal flares",
        lighting="controlled, motivated key with deep negative fill; clean highlights",
        grade="cool teal shadows with warm skin, high contrast, filmic",
        camera_bias="locked-off, deliberate, slow",
        symmetric=True,
        note="centred, symmetrical, anamorphic — every frame composed like a painting",
    ),
    "naturalistic_handheld": StyleProfile(
        name="naturalistic_handheld",
        lens="35mm spherical, naturalistic depth, available-light feel",
        lighting="soft naturalistic daylight, practical sources, gentle wrap",
        grade="muted naturalistic palette, low contrast, true skin tones",
        camera_bias="loose handheld, breathing frame, observational",
        symmetric=False,
        note="documentary-naturalistic — a loose observational handheld eye",
    ),
    "noir_chiaroscuro": StyleProfile(
        name="noir_chiaroscuro",
        lens="50mm spherical, deep focus, hard edges",
        lighting="hard low-key chiaroscuro, single hard key, venetian-blind shadows, deep blacks",
        grade="high-contrast monochrome-leaning grade, crushed shadows, silver highlights",
        camera_bias="composed, low and canted, deliberate",
        symmetric=False,
        note="film-noir chiaroscuro — hard shadow, deep black, moral unease in the light",
    ),
    "epic_vista": StyleProfile(
        name="epic_vista",
        lens="wide 24mm, deep focus, vast depth",
        lighting="grand natural light, golden-hour rim, atmospheric haze and god-rays",
        grade="rich saturated earth-and-gold palette, painterly contrast",
        camera_bias="sweeping crane and slow push, stately",
        symmetric=False,
        note="epic-fantasy vista — sweeping scale, golden light, painterly grandeur",
    ),
    "romantic_soft": StyleProfile(
        name="romantic_soft",
        lens="85mm portrait, very shallow depth, creamy bokeh",
        lighting="soft warm window light, gentle backlight halo, low contrast",
        grade="warm rosy palette, lifted blacks, gentle bloom",
        camera_bias="gentle slow push and drift, intimate",
        symmetric=False,
        note="romantic-soft — warm, shallow, glowing intimacy",
    ),
    "kinetic_action": StyleProfile(
        name="kinetic_action",
        lens="28mm spherical, deep focus, fast and reactive",
        lighting="high-energy contrast light, hard sun or hard practicals, strong rim",
        grade="punchy saturated grade, crisp contrast, cool steel and warm fire",
        camera_bias="fast tracking and whip pans, urgent handheld",
        symmetric=False,
        note="kinetic-action — urgent tracking, hard light, punchy contrast",
    ),
    "classical_balanced": StyleProfile(
        name="classical_balanced",
        lens="40mm spherical, balanced depth of field",
        lighting="classic three-point lighting, soft key, motivated fill",
        grade="balanced natural palette, gentle contrast, true colour",
        camera_bias="steady, composed, unobtrusive",
        symmetric=False,
        note="classical-balanced — the unobtrusive, well-composed default eye",
    ),
}

#: The genre → profile default (the directorial eye a genre is shot through).
_GENRE_PROFILE: dict[Genre, str] = {
    Genre.NOIR: "noir_chiaroscuro",
    Genre.HORROR: "noir_chiaroscuro",
    Genre.ACTION: "kinetic_action",
    Genre.ROMANCE: "romantic_soft",
    Genre.FANTASY: "epic_vista",
    Genre.DRAMA: "naturalistic_handheld",
    Genre.NEUTRAL: "classical_balanced",
}


def select_style_profile(
    beats: Sequence[Beat],
    *,
    style_tokens: dict[str, object] | None = None,
    override: str | None = None,
) -> StyleProfile:
    """Pick the directorial eye for a scene (pure, deterministic).

    Precedence: an explicit ``override`` (a canon style token or a director ask)
    wins; otherwise an ``aesthetic``/``director_style`` style token names a
    profile directly; otherwise the dominant genre selects its default eye.
    Unknown names fall back to ``classical_balanced`` so the look never breaks.
    """
    if override and override in STYLE_PROFILES:
        return STYLE_PROFILES[override]
    if style_tokens:
        for key in ("director_style", "aesthetic", "profile", "look"):
            named = style_tokens.get(key)
            if isinstance(named, str) and named in STYLE_PROFILES:
                return STYLE_PROFILES[named]
    return STYLE_PROFILES[_GENRE_PROFILE[infer_genre(beats)]]


#: Director-note phrases that name a profile directly (the §8.6 style ask). Each
#: entry maps a free-text cue to a profile id, so "shoot it like noir" → noir.
_STYLE_NOTE_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "anamorphic_symmetry",
        ("symmetrical", "symmetry", "anamorphic", "wes anderson", "centred", "centered"),
    ),
    (
        "naturalistic_handheld",
        ("handheld", "naturalistic", "documentary", "observational", "loose camera", "shaky"),
    ),
    (
        "noir_chiaroscuro",
        ("noir", "chiaroscuro", "high contrast shadows", "hard shadow", "film noir"),
    ),
    ("epic_vista", ("epic", "sweeping", "grand", "vista", "lord of the rings", "painterly")),
    ("romantic_soft", ("romantic", "dreamy", "soft glow", "warm and soft", "glowing")),
    ("kinetic_action", ("kinetic", "punchy", "fast cuts", "action-movie", "high energy")),
)


def infer_style_override(note: str) -> str | None:
    """Map a free-text director note to a style-profile id, or ``None`` (§8.6 bridge).

    The §8.6 preference loop learns *axis* priors (pacing/palette) on the memory
    side; this is the cinematographer-side bridge for a note that names a whole
    *look* ("shoot it like noir", "more symmetrical, Wes-Anderson style"). The
    matched id can be passed as ``select_style_profile(..., override=...)`` so a
    style ask re-shoots the scene through a different eye. ``None`` when the note
    names no known look (the genre default stands).
    """
    text = note.lower()
    for profile_id, cues in _STYLE_NOTE_CUES:
        if any(cue in text for cue in cues):
            return profile_id
    return None


def style_prompt_fragment(profile: StyleProfile) -> str:
    """Render a profile as a single prompt clause (the film's constant look)."""
    composition = "symmetrical centred composition" if profile.symmetric else "composed framing"
    return (
        f"shot on {profile.lens}; {profile.lighting}; {profile.grade}; "
        f"{composition}, {profile.camera_bias}"
    )


# --------------------------------------------------------------------------- #
# Lens / lighting / colour-grade — derived from profile, nudged by the beat
# --------------------------------------------------------------------------- #

#: Mood nudges layered *over* the profile default (a beat-level override). A
#: tender beat softens the key even inside a noir scene; a tense beat hardens it.
_MOOD_LIGHTING: dict[Mood, str] = {
    Mood.TENSE: "harder key, deeper shadow, raised contrast for unease",
    Mood.EERIE: "cold under-light, pooled shadow, an unnatural single source",
    Mood.TENDER: "softer wrap, gentle backlight halo, lifted shadow",
    Mood.SOMBER: "low, flat, desaturated light, weight in the shadows",
    Mood.TRIUMPHANT: "bright rim and lifted key, light breaking through",
    Mood.CALM: "even soft light, gentle gradients, settled exposure",
    Mood.NEUTRAL: "",
}

_MOOD_GRADE: dict[Mood, str] = {
    Mood.TENSE: "cooler, higher contrast",
    Mood.EERIE: "sickly green-cyan cast, crushed blacks",
    Mood.TENDER: "warmer, softer, lifted blacks",
    Mood.SOMBER: "desaturated, muted, blue-grey",
    Mood.TRIUMPHANT: "warm golden, rich saturation",
    Mood.CALM: "balanced, soft, naturalistic",
    Mood.NEUTRAL: "",
}

#: Intimate / detail beats want a longer, shallower lens regardless of profile.
_INTIMATE_CUES = ("face", "eyes", "hands", "whisper", "tears", "trembling", "lips", "close")
#: Vista / scale beats want a wide, deep lens regardless of profile.
_VISTA_CUES = ("vista", "landscape", "horizon", "skyline", "panorama", "vast", "expanse", "wide")


def lens_for(beat: Beat, profile: StyleProfile) -> str:
    """The lens for a beat: the profile's lens, overridden by intimate/vista cues."""
    text = _beat_text(beat)
    if any(cue in text for cue in _INTIMATE_CUES):
        return "85mm portrait lens, very shallow depth of field, creamy bokeh isolating the subject"
    if any(cue in text for cue in _VISTA_CUES):
        return "wide 24mm lens, deep focus holding the whole vista in sharp depth"
    return profile.lens


def lighting_for(beat: Beat, profile: StyleProfile) -> str:
    """The lighting for a beat: the profile key, then the beat's mood nudge."""
    nudge = _MOOD_LIGHTING.get(infer_mood(beat), "")
    return f"{profile.lighting}; {nudge}" if nudge else profile.lighting


def color_grade_for(beat: Beat, profile: StyleProfile) -> str:
    """The colour grade for a beat: the profile grade, then the beat's mood nudge."""
    nudge = _MOOD_GRADE.get(infer_mood(beat), "")
    return f"{profile.grade}; {nudge}" if nudge else profile.grade


# --------------------------------------------------------------------------- #
# Scene coverage — the master / medium / CU set
# --------------------------------------------------------------------------- #


class CoverageRole(StrEnum):
    """The role a covered angle plays in the edit (the classic coverage set)."""

    MASTER = "master"  # the wide that establishes geography and holds everything
    MEDIUM = "medium"  # the mid that carries the scene's action
    CLOSE = "close"  # the insert / single that lands emotion and detail
    REVERSE = "reverse"  # the answering single in a two-hander (eyeline match)


@dataclass(frozen=True, slots=True)
class CoverageShot:
    """One angle in a scene's coverage plan (an editorial intention, not a render)."""

    role: CoverageRole
    shot_size: str
    subject: str | None = None
    note: str = ""


def plan_coverage(
    beats: Sequence[Beat], *, subjects: Sequence[str] | None = None
) -> list[CoverageShot]:
    """Plan a scene's coverage set: a master, mediums, and close inserts (pure).

    Real coverage is *planned* before it is cut: a scene opens on a **master**
    wide that establishes geography, plays its action in **mediums**, and lands
    its emotional beats in **close** inserts. When two or more ``subjects`` are
    present the plan adds the answering **reverse** singles that a two-hander is
    cut from (the shot/reverse-shot pair). The per-beat sizes come from the §10
    shot-size grammar so coverage and the event rhythm agree.
    """
    if not beats:
        return []
    sizes = shot_sizes_for_event(beats)
    people = list(subjects or [])
    plan: list[CoverageShot] = [
        CoverageShot(CoverageRole.MASTER, "wide", note="establish geography; hold the whole space")
    ]
    for ordinal, size in enumerate(sizes):
        if ordinal == 0:
            continue  # the opening beat is the master we already added
        if size == "close":
            subject = people[ordinal % len(people)] if people else None
            plan.append(
                CoverageShot(CoverageRole.CLOSE, "close", subject=subject, note="land the emotion")
            )
        else:
            plan.append(CoverageShot(CoverageRole.MEDIUM, size, note="carry the action"))
    # A two-hander is covered with answering reverses (the eyeline-matched singles).
    if len(people) >= 2:
        plan.append(
            CoverageShot(
                CoverageRole.CLOSE, "close", subject=people[0], note="single, eyeline right"
            )
        )
        plan.append(
            CoverageShot(
                CoverageRole.REVERSE,
                "close",
                subject=people[1],
                note="reverse single, eyeline left",
            )
        )
    return plan


# --------------------------------------------------------------------------- #
# Shot / reverse-shot + eyeline match (the two-hander)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReverseShotPair:
    """An over-the-shoulder shot/reverse-shot pair with matched eyelines (§10).

    Two people in conversation are filmed on **one side of the action line** (the
    eyeline axis between them): A looks frame-right, B looks frame-left, and the
    camera never crosses to the other side (which would swap who-looks-where and
    break the conversation). ``axis`` is the resolved screen direction the pair is
    pinned to so the Continuity QA's 180° check (``violates_180``) shares it.
    """

    subject_a: str
    subject_b: str
    #: A frames looking this way; B frames the opposite way (the eyeline match).
    a_looks: ScreenDirection
    b_looks: ScreenDirection
    axis: ScreenDirection


def _eyeline_opposite(direction: ScreenDirection) -> ScreenDirection:
    return (
        ScreenDirection.RIGHT_TO_LEFT
        if direction is ScreenDirection.LEFT_TO_RIGHT
        else ScreenDirection.LEFT_TO_RIGHT
    )


def shot_reverse_shot(
    subject_a: str,
    subject_b: str,
    *,
    axis: ScreenDirection = ScreenDirection.LEFT_TO_RIGHT,
) -> ReverseShotPair:
    """Build the eyeline-matched shot/reverse-shot pair for a two-hander (pure).

    A is placed looking along ``axis`` (left-to-right ⇒ A looks frame-right); the
    reverse on B looks the opposite way so their gazes meet across the cut. The
    axis is held — the camera stays on one side of the line — so the QA's
    :func:`app.render.shot_grammar.violates_180` reads a non-violating, motivated
    pair.
    """
    if axis not in (ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.RIGHT_TO_LEFT):
        axis = ScreenDirection.LEFT_TO_RIGHT
    a_looks = axis
    b_looks = _eyeline_opposite(axis)
    return ReverseShotPair(
        subject_a=subject_a,
        subject_b=subject_b,
        a_looks=a_looks,
        b_looks=b_looks,
        axis=axis,
    )


# --------------------------------------------------------------------------- #
# Character blocking — where the subject sits in frame
# --------------------------------------------------------------------------- #


class FramePosition(StrEnum):
    """Which third of the frame the subject is blocked into (composition)."""

    LEFT = "left_third"
    CENTER = "center"
    RIGHT = "right_third"


@dataclass(frozen=True, slots=True)
class Blocking:
    """A subject's frame position + the lead room implied by its screen direction."""

    subject: str | None
    position: FramePosition
    #: The empty "lead room" the subject moves/looks into (or ``None`` if centred).
    lead_room: FramePosition | None = None


def _lead_position(direction: ScreenDirection, symmetric: bool) -> Blocking:
    """Block one subject from its resolved screen direction (with lead room)."""
    centred = (
        ScreenDirection.NEUTRAL,
        ScreenDirection.TOWARD,
        ScreenDirection.AWAY,
    )
    if symmetric or direction in centred:
        return Blocking(subject=None, position=FramePosition.CENTER, lead_room=None)
    if direction is ScreenDirection.LEFT_TO_RIGHT:
        # Moving right ⇒ sit left-of-centre with lead room ahead (frame-right).
        return Blocking(subject=None, position=FramePosition.LEFT, lead_room=FramePosition.RIGHT)
    return Blocking(subject=None, position=FramePosition.RIGHT, lead_room=FramePosition.LEFT)


def block_subjects(
    beats: Sequence[Beat],
    *,
    profile: StyleProfile | None = None,
) -> list[Blocking]:
    """Block the subject of each beat into a frame third (pure, per-event).

    Blocking follows the resolved screen direction so composition and continuity
    agree: a subject moving left-to-right sits in the left third with lead room to
    frame-right (you compose *into* the move). A symmetric profile (anamorphic)
    centres the subject regardless — its grammar is balance, not lead room. A
    neutral/toward/away beat is centred.
    """
    symmetric = bool(profile and profile.symmetric)
    directions = resolve_screen_directions(beats)
    return [_lead_position(direction, symmetric) for direction in directions]


# --------------------------------------------------------------------------- #
# Visual rhythm / shot-length cadence
# --------------------------------------------------------------------------- #

#: The cadence band: a relaxed scene holds longer; a building/tense one tightens.
RELAXED_BASE_S = 6.0
DRIVING_BASE_S = 3.5
#: How much each successive shot tightens as the scene builds (multiplicative).
_BUILD_TIGHTEN = 0.88
#: Floor / ceiling for a single shot's cadence length (seconds).
MIN_CADENCE_S = 2.0
MAX_CADENCE_S = 8.0


@dataclass(frozen=True, slots=True)
class Cadence:
    """The visual-rhythm plan for an event: per-shot lengths + whether it builds."""

    lengths_s: list[float] = field(default_factory=list)
    building: bool = False


#: Cues that make a scene *accelerate* (shorten shots toward the climax).
_BUILD_CUES = ("chase", "sprint", "fight", "panic", "frantic", "escape", "battle", "race", "urgent")
#: Cues that make a scene *hold* (long, lingering shots).
_HOLD_CUES = ("calm", "quiet", "still", "linger", "languid", "gentle", "somber", "peaceful")


def _scene_builds(beats: Sequence[Beat]) -> bool:
    text = " ".join(_beat_text(b) for b in beats)
    return any(cue in text for cue in _BUILD_CUES)


def _scene_holds(beats: Sequence[Beat]) -> bool:
    text = " ".join(_beat_text(b) for b in beats)
    return any(cue in text for cue in _HOLD_CUES)


def shot_length_cadence(beats: Sequence[Beat]) -> Cadence:
    """The shot-length pattern an event plays (pure, deterministic).

    A driving scene (chase/fight) starts shorter and **tightens** shot-to-shot so
    the cut accelerates into the climax; a holding scene (calm/quiet) plays long,
    even shots; a default scene plays the base length flat. The lengths are
    clamped to the ``[MIN, MAX]`` cadence band so no single shot runs away. This
    is the rhythm the Event Director's per-shot duration reads, layered over the
    reading-paced estimate.
    """
    if not beats:
        return Cadence(lengths_s=[], building=False)
    building = _scene_builds(beats)
    holding = _scene_holds(beats) and not building
    base = DRIVING_BASE_S if building else (RELAXED_BASE_S if holding else 5.0)
    lengths: list[float] = []
    current = base
    for ordinal in range(len(beats)):
        if building and ordinal > 0:
            current *= _BUILD_TIGHTEN  # accelerate toward the climax
        lengths.append(round(max(MIN_CADENCE_S, min(MAX_CADENCE_S, current)), 2))
    return Cadence(lengths_s=lengths, building=building)


# --------------------------------------------------------------------------- #
# Camera derivation — fold the cinematic language into a Camera block
# --------------------------------------------------------------------------- #

#: A symmetric profile prefers static/centred holds; a kinetic one prefers motion.
_PROFILE_MOVE: dict[str, str] = {
    "anamorphic_symmetry": "static",
    "noir_chiaroscuro": "static",
    "naturalistic_handheld": "handheld",
    "epic_vista": "crane",
    "romantic_soft": "push_in",
    "kinetic_action": "track",
    "classical_balanced": "pan",
}
#: Camera biases that read as slow operating temperaments.
_SLOW_BIASES = ("locked", "gentle", "sweeping")
_POSE_CUES = ("freeze", "lands on", "comes to rest", "stops dead", "final")


def camera_for_beat(
    ordinal: int,
    beat: Beat,
    profile: StyleProfile,
    *,
    cadence: Cadence | None = None,
) -> Camera:
    """Derive a full :class:`Camera` for a beat from the cinematic-language model.

    Shot size comes from the §10 grammar (establishing → medium → insert); the
    move is the profile's operating temperament unless the beat lands a pose
    (static) or opens the scene (push in on the establishing wide); the speed
    follows the cadence (a building scene reads fast, a slow-temperament profile
    reads slow).
    """
    size = shot_size_for(ordinal, beat)
    text = _beat_text(beat)
    pose = any(cue in text for cue in _POSE_CUES)
    move = "static" if pose and ordinal > 0 else _PROFILE_MOVE.get(profile.name, "pan")
    if ordinal == 0 and not pose:
        move = "push_in"  # open the scene moving in on the establishing wide
    building = bool(cadence and cadence.building)
    slow = profile.camera_bias.startswith(_SLOW_BIASES)
    speed = "fast" if building else ("slow" if slow else "medium")
    return Camera(move=move, speed=speed, shot_size=size)


# --------------------------------------------------------------------------- #
# Scene sequencer — tie the whole cinematic language into one plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ShotPlan:
    """The full cinematic-language plan for one beat (a single shot).

    Everything a downstream prompt or render directive needs to shoot the beat
    deliberately: its place in the rhythm (``shot_size``/``camera``/``length_s``),
    its look (``lens``/``lighting``/``grade``), and its continuity (the resolved
    ``screen_direction`` + the subject's ``blocking``).
    """

    ordinal: int
    beat_id: str
    shot_size: str
    camera: Camera
    lens: str
    lighting: str
    grade: str
    screen_direction: ScreenDirection
    blocking: Blocking
    mood: str
    length_s: float


@dataclass(frozen=True, slots=True)
class ScenePlan:
    """The cinematographer's deterministic plan for a whole scene/event.

    Bundles the directorial eye (``profile``/``genre``), the per-shot
    :class:`ShotPlan` list, the editorial ``coverage`` set, and any detected
    ``axis_violations`` (the 180° errors the continuity QA should flag). A scene
    plan is pure and serializable — the single source of truth the Cinematographer
    agent, the Event Director, and the Continuity QA can all read.
    """

    profile: StyleProfile
    genre: Genre
    shots: list[ShotPlan] = field(default_factory=list)
    coverage: list[CoverageShot] = field(default_factory=list)
    axis_violations: list[AxisViolation] = field(default_factory=list)


def plan_scene(
    beats: Sequence[Beat],
    *,
    style_tokens: dict[str, object] | None = None,
    subjects: Sequence[str] | None = None,
    profile_override: str | None = None,
) -> ScenePlan:
    """Compose a whole scene's cinematic plan from its beats (pure, deterministic).

    Selects one directorial eye for the scene, then walks the beats resolving each
    one's shot size, camera, lens/lighting/grade, screen direction, and blocking —
    so the scene reads as a deliberate, consistent cut sequence. Also returns the
    editorial coverage set and any 180° axis violations. This is the high-level
    entry point that exercises every layer of the model together.
    """
    beat_list = list(beats)
    profile = select_style_profile(beat_list, style_tokens=style_tokens, override=profile_override)
    genre = infer_genre(beat_list)
    if not beat_list:
        return ScenePlan(profile=profile, genre=genre)

    cadence = shot_length_cadence(beat_list)
    directions = resolve_screen_directions(beat_list)
    blocks = block_subjects(beat_list, profile=profile)
    sizes = shot_sizes_for_event(beat_list)

    shots: list[ShotPlan] = []
    for ordinal, beat in enumerate(beat_list):
        length = cadence.lengths_s[ordinal] if ordinal < len(cadence.lengths_s) else 5.0
        camera = camera_for_beat(ordinal, beat, profile, cadence=cadence)
        # An expressive (genre, mood) move overrides the profile temperament when
        # one is motivated — a dolly-zoom for horror tension, a whip-pan for action.
        expressive = expressive_move_for(beat, genre)
        if expressive is not None and ordinal != 0:
            camera = camera.model_copy(update={"move": expressive})
        shots.append(
            ShotPlan(
                ordinal=ordinal,
                beat_id=beat.beat_id or f"beat_{ordinal:02d}",
                shot_size=sizes[ordinal],
                camera=camera,
                lens=lens_for(beat, profile),
                lighting=lighting_for(beat, profile),
                grade=color_grade_for(beat, profile),
                screen_direction=directions[ordinal],
                blocking=blocks[ordinal],
                mood=infer_mood(beat).value,
                length_s=length,
            )
        )
    return ScenePlan(
        profile=profile,
        genre=genre,
        shots=shots,
        coverage=plan_coverage(beat_list, subjects=subjects),
        axis_violations=detect_axis_violations(beat_list),
    )


# --------------------------------------------------------------------------- #
# Prompt-fragment compiler — the deterministic look, straight into the prompt
# --------------------------------------------------------------------------- #

#: A human-readable phrasing for each frame third (read into a composition clause).
_BLOCKING_PHRASE: dict[FramePosition, str] = {
    FramePosition.LEFT: "subject framed left of centre",
    FramePosition.CENTER: "subject centred",
    FramePosition.RIGHT: "subject framed right of centre",
}
#: How each shot size reads as a framing clause.
_SIZE_PHRASE: dict[str, str] = {
    "extreme_wide": "extreme wide shot, the figure small in a vast frame",
    "wide": "wide establishing shot holding the whole space",
    "medium": "medium shot from the waist up",
    "close": "close-up, the face filling the frame",
    "extreme_close": "extreme close-up on a single detail",
}
#: How each camera move reads as a motion clause (speed-neutral — the speed
#: prefix is added by :func:`move_phrase` so it is never doubled).
_MOVE_PHRASE: dict[str, str] = {
    "static": "locked-off static frame",
    "push_in": "push-in",
    "pull_out": "pull-back reveal",
    "pan": "pan",
    "tilt": "tilt",
    "track": "tracking move following the action",
    "orbit": "orbiting arc around the subject",
    "crane": "rising crane move",
    "handheld": "handheld move",
    "zoom_in": "zoom in",
    "zoom_out": "zoom out",
}


def _move_phrase(camera: Camera) -> str:
    return move_phrase(camera.move, camera.speed)


def compile_shot_prompt(plan: ShotPlan, *, subject: str | None = None) -> str:
    """Compile a :class:`ShotPlan` into one deterministic prompt clause (pure).

    Turns the *decided* cinematic language — framing, camera move, lens, lighting,
    grade, blocking — into the prompt string the Wan/i2v model actually reads, so
    the consistent look lives in the prompt itself, not only in the LLM's
    discretion. The ``subject`` (a locked character name) is named when given so
    the framing clause has someone to frame. Clauses are joined with ``; `` and
    every fragment is concrete, never generic.
    """
    who = subject or "the subject"
    framing = _SIZE_PHRASE.get(plan.shot_size, f"{plan.shot_size} shot")
    blocking = _BLOCKING_PHRASE[plan.blocking.position].replace("subject", who, 1)
    clauses = [
        f"{framing}, {blocking}",
        _move_phrase(plan.camera),
        plan.lens,
        plan.lighting,
        plan.grade,
    ]
    if plan.blocking.lead_room is not None:
        side = "frame-right" if plan.blocking.lead_room is FramePosition.RIGHT else "frame-left"
        clauses.append(f"lead room to {side}")
    return "; ".join(c for c in clauses if c)


def compile_scene_prompts(plan: ScenePlan, *, subject: str | None = None) -> list[str]:
    """Compile every shot of a :class:`ScenePlan` into its prompt clause (pure)."""
    return [compile_shot_prompt(shot, subject=subject) for shot in plan.shots]


# --------------------------------------------------------------------------- #
# Lens / grade continuity guard — the unmotivated "focal-length pop"
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Negative-prompt grammar — the deterministic "do not render this" floor
# --------------------------------------------------------------------------- #

#: The universal artifact floor every shot carries (the §10 Cinematographer list).
_BASE_NEGATIVE = (
    "extra fingers",
    "warped face",
    "deformed hands",
    "lifeless eyes",
    "stiff frozen pose",
    "flicker",
    "hard cuts",
    "watermark",
    "text",
    "low detail",
    "blurry",
)
#: Per-genre additions — the things that specifically break *that* genre's look.
_GENRE_NEGATIVE: dict[Genre, tuple[str, ...]] = {
    Genre.NOIR: ("flat lighting", "washed-out colours", "modern objects", "bright daylight"),
    Genre.HORROR: ("comedic expression", "bright cheerful palette", "modern objects"),
    Genre.ACTION: ("motion smear", "morphing limbs", "static lifeless frame"),
    Genre.FANTASY: ("modern objects", "contemporary clothing", "plastic textures"),
    Genre.ROMANCE: ("harsh shadows", "clinical lighting", "grimace"),
    Genre.DRAMA: ("over-saturated", "cartoonish", "exaggerated expression"),
    Genre.NEUTRAL: (),
}


def negative_prompt_for(beats: Sequence[Beat], *, genre: Genre | None = None) -> str:
    """The deterministic negative prompt for a scene (base floor + genre rules).

    A comma-joined list the Cinematographer can carry verbatim as a floor (the
    LLM may add to it, never drop it): every shot avoids the universal artifacts,
    and a genre adds the look-breakers specific to it (a noir clip must not go
    bright-daylight; a fantasy clip must not show modern objects). Pure.
    """
    resolved = genre if genre is not None else infer_genre(list(beats))
    parts = list(_BASE_NEGATIVE) + list(_GENRE_NEGATIVE.get(resolved, ()))
    # De-dup while preserving order (a genre rule may echo a base one).
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            ordered.append(part)
    return ", ".join(ordered)


# --------------------------------------------------------------------------- #
# Camera-move vocabulary — mood/genre-aware expressive moves
# --------------------------------------------------------------------------- #

#: Expressive moves keyed by an (genre, mood) intent, layered over the profile's
#: default temperament. Each is a real cinematographic move with a clear meaning.
_EXPRESSIVE_MOVES: dict[tuple[Genre, Mood], str] = {
    (Genre.HORROR, Mood.EERIE): "creeping_push_in",
    (Genre.HORROR, Mood.TENSE): "slow_dolly_zoom",
    (Genre.ACTION, Mood.TENSE): "whip_pan",
    (Genre.ACTION, Mood.TRIUMPHANT): "rising_crane",
    (Genre.ROMANCE, Mood.TENDER): "gentle_orbit",
    (Genre.DRAMA, Mood.SOMBER): "slow_pull_out",
    (Genre.FANTASY, Mood.TRIUMPHANT): "sweeping_crane",
}
#: How each expressive move reads as a prompt motion clause.
_EXPRESSIVE_MOVE_PHRASE: dict[str, str] = {
    "creeping_push_in": "a creeping, almost imperceptible push-in building dread",
    "slow_dolly_zoom": "a slow dolly-zoom (vertigo effect) warping the depth",
    "whip_pan": "a fast whip-pan snapping to the action",
    "rising_crane": "a rising crane move lifting over the scene",
    "gentle_orbit": "a gentle orbit drifting around the subject",
    "slow_pull_out": "a slow pull-back isolating the figure in empty space",
    "sweeping_crane": "a sweeping crane revealing the full scale of the vista",
}


def expressive_move_for(beat: Beat, genre: Genre) -> str | None:
    """An expressive camera move for a beat's genre+mood, or ``None`` (use default).

    Maps the (genre, mood) intent to a real cinematographic move — a dolly-zoom
    for horror tension, a whip-pan for action, a gentle orbit for a tender
    romance beat — so the move *means* something. ``None`` falls back to the
    profile's default temperament (:func:`camera_for_beat`).
    """
    return _EXPRESSIVE_MOVES.get((genre, infer_mood(beat)))


def move_phrase(move: str, speed: str = "medium") -> str:
    """Render any move id (default or expressive) as a prompt motion clause."""
    if move in _EXPRESSIVE_MOVE_PHRASE:
        return _EXPRESSIVE_MOVE_PHRASE[move]
    base = _MOVE_PHRASE.get(move, move.replace("_", " "))
    if speed and speed != "medium" and move != "static":
        return f"{speed} {base}"
    return base


class LookJumpKind(StrEnum):
    """The kind of unmotivated look discontinuity between two consecutive shots."""

    LENS = "lens"  # an unmotivated focal-length pop
    GRADE = "grade"  # an unmotivated colour-grade jump


@dataclass(frozen=True, slots=True)
class LookJump:
    """One unmotivated lens/grade discontinuity at a seam (the later shot's index).

    A scene shot through one eye holds its lens and grade; a *motivated* change
    (an intimate insert pulling to an 85mm, a mood flip recolouring the grade) is
    fine, but an unmotivated swap reads as a continuity error. ``ordinal`` is the
    later shot; ``prev``/``cur`` are the two values that jumped.
    """

    ordinal: int
    kind: LookJumpKind
    prev: str
    cur: str


def detect_look_jumps(plan: ScenePlan) -> list[LookJump]:
    """Find unmotivated lens/grade jumps across a scene's shots (pure).

    A lens change is *motivated* when the shot size changes (you expect a longer
    lens on a close insert) or the mood changes; a grade change is motivated when
    the mood changes. Anything else — same size, same mood, but a different lens
    or grade — is the unmotivated pop this guard flags so a repair can re-pin the
    look. The continuity QA can route these the way it routes a 180° violation.
    """
    jumps: list[LookJump] = []
    shots = plan.shots
    for ordinal in range(1, len(shots)):
        prev, cur = shots[ordinal - 1], shots[ordinal]
        size_changed = prev.shot_size != cur.shot_size
        mood_changed = prev.mood != cur.mood
        if prev.lens != cur.lens and not size_changed and not mood_changed:
            jumps.append(LookJump(ordinal, LookJumpKind.LENS, prev.lens, cur.lens))
        if prev.grade != cur.grade and not mood_changed:
            jumps.append(LookJump(ordinal, LookJumpKind.GRADE, prev.grade, cur.grade))
    return jumps


# --------------------------------------------------------------------------- #
# Transition grammar — the cut/dissolve/fade between two moments
# --------------------------------------------------------------------------- #


class Transition(StrEnum):
    """How one shot/scene gives way to the next (the editorial transition)."""

    CUT = "cut"  # the default, invisible hard cut
    DISSOLVE = "dissolve"  # a soft cross-dissolve (time passing, dream, memory)
    FADE_TO_BLACK = "fade_to_black"  # a full close — end of a movement / chapter
    FADE_FROM_BLACK = "fade_from_black"  # an open after a fade-out
    MATCH_CUT = "match_cut"  # a graphic match (shape/motion carries across the cut)
    SMASH_CUT = "smash_cut"  # an abrupt jolt into a contrasting moment


#: Default transition seconds per kind (a hard cut is instantaneous).
_TRANSITION_S: dict[Transition, float] = {
    Transition.CUT: 0.0,
    Transition.DISSOLVE: 0.6,
    Transition.FADE_TO_BLACK: 0.8,
    Transition.FADE_FROM_BLACK: 0.8,
    Transition.MATCH_CUT: 0.0,
    Transition.SMASH_CUT: 0.0,
}
#: Cues that justify a soft dissolve between moments (time / memory / dream).
_DISSOLVE_CUES = ("later", "hours pass", "days later", "meanwhile", "dream", "memory", "remembers")
#: Cues that justify a full fade (an ending, a sleep, a death, a chapter close).
_FADE_CUES = ("the end", "darkness", "sleep", "faded", "everything went black", "chapter")
#: Cues that justify a graphic match cut (a shape/motion carrying across).
_MATCH_CUES = ("just like", "echoing", "mirror", "as if", "the same")


def transition_between(prev: Beat, cur: Beat) -> Transition:
    """Pick the transition from ``prev`` into ``cur`` (pure, deterministic).

    Most cuts are invisible hard cuts; the grammar reaches for a soft transition
    only when the text motivates it — a time jump or memory dissolves, a chapter
    close fades, a graphic echo match-cuts, and a sudden contrast (calm → tense)
    smash-cuts. This is the seam the stitcher reads to choose its crossfade.
    """
    cur_text = _beat_text(cur)
    if any(cue in cur_text for cue in _FADE_CUES):
        return Transition.FADE_TO_BLACK
    if any(cue in cur_text for cue in _DISSOLVE_CUES):
        return Transition.DISSOLVE
    if any(cue in cur_text for cue in _MATCH_CUES):
        return Transition.MATCH_CUT
    # A hard tonal contrast (calm/tender → tense/eerie) reads as a smash cut.
    prev_mood, cur_mood = infer_mood(prev), infer_mood(cur)
    calm = {Mood.CALM, Mood.TENDER}
    jolt = {Mood.TENSE, Mood.EERIE}
    if prev_mood in calm and cur_mood in jolt:
        return Transition.SMASH_CUT
    return Transition.CUT


def transition_seconds(transition: Transition) -> float:
    """The default duration (seconds) of a transition for the stitcher/crossfade."""
    return _TRANSITION_S[transition]


def plan_transitions(beats: Sequence[Beat]) -> list[Transition]:
    """The transition *into* each beat (the first opens on a fade-from-black) (pure)."""
    if not beats:
        return []
    transitions = [Transition.FADE_FROM_BLACK]
    for ordinal in range(1, len(beats)):
        transitions.append(transition_between(beats[ordinal - 1], beats[ordinal]))
    return transitions


__all__ = [
    "Blocking",
    "Cadence",
    "CoverageRole",
    "CoverageShot",
    "DRIVING_BASE_S",
    "FramePosition",
    "Genre",
    "LookJump",
    "LookJumpKind",
    "MAX_CADENCE_S",
    "MIN_CADENCE_S",
    "Mood",
    "RELAXED_BASE_S",
    "ReverseShotPair",
    "STYLE_PROFILES",
    "ScenePlan",
    "ShotPlan",
    "StyleProfile",
    "Transition",
    "block_subjects",
    "camera_for_beat",
    "color_grade_for",
    "compile_scene_prompts",
    "compile_shot_prompt",
    "detect_look_jumps",
    "expressive_move_for",
    "infer_genre",
    "infer_mood",
    "infer_style_override",
    "lens_for",
    "lighting_for",
    "move_phrase",
    "negative_prompt_for",
    "plan_coverage",
    "plan_scene",
    "plan_transitions",
    "select_style_profile",
    "shot_length_cadence",
    "shot_reverse_shot",
    "style_prompt_fragment",
    "transition_between",
    "transition_seconds",
]
