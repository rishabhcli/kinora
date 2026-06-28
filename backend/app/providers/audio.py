"""Audio provider seam — music/SFX sources + the deterministic mix plan (§9.6).

This is the "music/SFX provider" half of audio post-production. Two concerns live
here, both pure data + pure planning (no ffmpeg, no model spend):

* **The mix vocabulary** — :class:`MixProfile` (the mastering target),
  :class:`SfxEvent` (a timed sound effect), :class:`MusicBedSpec` (how the score
  cue is laid under the narration), and :class:`MixPlan` (the resolved recipe the
  ffmpeg stage in :mod:`app.render.audio_post` executes).
* **The source seam** — :class:`MusicProvider` (a Protocol) and the default
  :class:`LocalCueLibrary` that synthesises a copyright-clean bed *descriptor* from a
  :class:`~app.render.music.ScoreCue`. Today the bed is generated procedurally by
  ffmpeg; a hosted music-gen provider (Phase 10) implements the same Protocol and
  drops in without changing the mix planner.

The mix is fully determined by its inputs (narration length, score cue, SFX list,
profile), so :func:`plan_mix` is a pure function and is unit-tested without audio.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.render.music import Mood, ScoreCue


class MasterPreset(StrEnum):
    """A named mastering target — the loudness/headroom feel of the final mix."""

    #: Balanced film mix: music present, speech clear. The default.
    CINEMATIC = "cinematic"
    #: Speech-forward: aggressive ducking, quiet bed (accessibility / ESL focus, §3).
    DIALOGUE_FORWARD = "dialogue_forward"
    #: Late-night / quiet-room: low overall loudness, gentle dynamics.
    QUIET_ROOM = "quiet_room"
    #: Trailer / hook: louder, punchier — used for the demo opener (§16).
    PUNCHY = "punchy"


@dataclass(frozen=True, slots=True)
class MixProfile:
    """The mastering target for one scene's audio (EBU R128 terms).

    Attributes:
        preset: the named feel this profile came from.
        target_lufs: integrated loudness target (streaming standard ≈ -16 LUFS).
        true_peak_db: ceiling for the final true-peak limiter (dBTP).
        loudness_range: target LRA (dynamic range, LU).
        music_gain_db: trim applied to the music bed before ducking.
        duck_ratio: side-chain compression ratio applied to the bed under speech.
        duck_threshold_db: level below which the bed is *not* ducked.
        duck_attack_ms / duck_release_ms: ducking envelope timing.
    """

    preset: MasterPreset
    target_lufs: float
    true_peak_db: float
    loudness_range: float
    music_gain_db: float
    duck_ratio: float
    duck_threshold_db: float
    duck_attack_ms: float
    duck_release_ms: float

    @classmethod
    def for_preset(cls, preset: MasterPreset) -> MixProfile:
        """The canonical profile for a named preset."""
        return _PROFILES[preset]


_PROFILES: dict[MasterPreset, MixProfile] = {
    MasterPreset.CINEMATIC: MixProfile(
        preset=MasterPreset.CINEMATIC,
        target_lufs=-16.0,
        true_peak_db=-1.5,
        loudness_range=11.0,
        music_gain_db=-6.0,
        duck_ratio=8.0,
        duck_threshold_db=-30.0,
        duck_attack_ms=20.0,
        duck_release_ms=300.0,
    ),
    MasterPreset.DIALOGUE_FORWARD: MixProfile(
        preset=MasterPreset.DIALOGUE_FORWARD,
        target_lufs=-16.0,
        true_peak_db=-1.5,
        loudness_range=7.0,
        music_gain_db=-12.0,
        duck_ratio=14.0,
        duck_threshold_db=-36.0,
        duck_attack_ms=12.0,
        duck_release_ms=220.0,
    ),
    MasterPreset.QUIET_ROOM: MixProfile(
        preset=MasterPreset.QUIET_ROOM,
        target_lufs=-20.0,
        true_peak_db=-2.0,
        loudness_range=6.0,
        music_gain_db=-10.0,
        duck_ratio=6.0,
        duck_threshold_db=-32.0,
        duck_attack_ms=25.0,
        duck_release_ms=400.0,
    ),
    MasterPreset.PUNCHY: MixProfile(
        preset=MasterPreset.PUNCHY,
        target_lufs=-14.0,
        true_peak_db=-1.0,
        loudness_range=9.0,
        music_gain_db=-3.0,
        duck_ratio=6.0,
        duck_threshold_db=-26.0,
        duck_attack_ms=15.0,
        duck_release_ms=250.0,
    ),
}


class SfxKind(StrEnum):
    """A small, copyright-clean SFX taxonomy (each is a synth-rendered transient)."""

    WHOOSH = "whoosh"  # a transition swell between shots
    IMPACT = "impact"  # a low thud for a beat landing
    SPARKLE = "sparkle"  # a bright shimmer for a wondrous beat
    RUMBLE = "rumble"  # a sustained low bed for tension
    CHIME = "chime"  # a soft single tone for a page-turn accent


@dataclass(frozen=True, slots=True)
class SfxEvent:
    """One timed sound effect over the scene timeline.

    Attributes:
        at_s: start time on the scene timeline (clamped into [0, duration]).
        kind: which synth transient to render.
        gain_db: trim for this hit relative to the bed.
        duration_s: how long the transient sounds.
    """

    at_s: float
    kind: SfxKind
    gain_db: float = -8.0
    duration_s: float = 0.6


@dataclass(frozen=True, slots=True)
class MusicBedSpec:
    """How a :class:`ScoreCue` is laid under the narration for one scene."""

    cue: ScoreCue
    duration_s: float
    gain_db: float
    #: Seconds of fade at the head/tail so the bed enters/leaves gracefully.
    fade_in_s: float = 1.2
    fade_out_s: float = 1.6


@dataclass(frozen=True, slots=True)
class MixPlan:
    """The resolved, deterministic recipe the ffmpeg master stage executes.

    Pure data: given the same narration duration, cue, SFX, and profile, the same
    plan is produced. :mod:`app.render.audio_post` turns this into real audio.
    """

    duration_s: float
    profile: MixProfile
    bed: MusicBedSpec
    sfx: tuple[SfxEvent, ...] = ()

    @property
    def has_music(self) -> bool:
        return self.bed.cue.intensity > 0.0 and self.duration_s > 0.0


# --------------------------------------------------------------------------- #
# The music source seam
# --------------------------------------------------------------------------- #


@runtime_checkable
class MusicProvider(Protocol):
    """A source of a music-bed descriptor for a scored cue.

    The default :class:`LocalCueLibrary` returns the cue unchanged (the bed is then
    synthesised procedurally by the ffmpeg stage). A hosted music-gen provider
    (Phase 10) implements the same method, returning a cue whose ``timbre`` /
    pitches it actually rendered — and may attach pre-rendered bytes out of band.
    """

    def resolve_bed(self, cue: ScoreCue, *, duration_s: float) -> MusicBedSpec:
        """Resolve how ``cue`` is laid under a ``duration_s``-second scene."""
        ...


@dataclass(frozen=True, slots=True)
class LocalCueLibrary:
    """Default :class:`MusicProvider`: lay the cue as a procedurally-synth bed.

    Copyright-clean (no third-party recording) and deterministic. The cue's
    ``intensity`` maps to a bed gain so a tense scene's drone sits louder than a
    calm pad, before the profile's ducking pulls it under speech.
    """

    #: Loudest the bed is ever placed pre-duck (dB); intensity scales toward it.
    max_gain_db: float = -8.0
    #: Quietest a non-silent bed is placed (dB).
    min_gain_db: float = -22.0

    def resolve_bed(self, cue: ScoreCue, *, duration_s: float) -> MusicBedSpec:
        span = self.max_gain_db - self.min_gain_db
        gain = self.min_gain_db + span * max(0.0, min(1.0, cue.intensity))
        return MusicBedSpec(cue=cue, duration_s=max(0.0, duration_s), gain_db=round(gain, 2))


# --------------------------------------------------------------------------- #
# The pure mix planner
# --------------------------------------------------------------------------- #


def plan_mix(
    *,
    duration_s: float,
    cue: ScoreCue,
    profile: MixProfile,
    sfx: list[SfxEvent] | None = None,
    music: MusicProvider | None = None,
) -> MixPlan:
    """Assemble the deterministic :class:`MixPlan` for one scene (pure).

    Args:
        duration_s: the scene's narration/clip length the bed is fit to.
        cue: the :class:`ScoreCue` the scene scored to (:mod:`app.render.music`).
        profile: the :class:`MixProfile` mastering target.
        sfx: timed SFX events; out-of-range / negative-time events are clamped and
            sorted so the ffmpeg stage lays them in order.
        music: the music source; defaults to :class:`LocalCueLibrary`.

    The profile's ``music_gain_db`` is folded onto the library's intensity-derived
    gain so a dialogue-forward preset's bed sits quieter regardless of cue.
    """
    duration = max(0.0, duration_s)
    library = music or LocalCueLibrary()
    base_bed = library.resolve_bed(cue, duration_s=duration)
    bed = MusicBedSpec(
        cue=base_bed.cue,
        duration_s=base_bed.duration_s,
        gain_db=round(base_bed.gain_db + profile.music_gain_db, 2),
        fade_in_s=min(base_bed.fade_in_s, duration / 2 if duration else base_bed.fade_in_s),
        fade_out_s=min(base_bed.fade_out_s, duration / 2 if duration else base_bed.fade_out_s),
    )
    clamped_sfx = tuple(
        sorted(
            (
                SfxEvent(
                    at_s=round(max(0.0, min(event.at_s, duration)), 3),
                    kind=event.kind,
                    gain_db=event.gain_db,
                    duration_s=event.duration_s,
                )
                for event in (sfx or [])
                if duration > 0.0 and event.at_s < duration
            ),
            key=lambda e: e.at_s,
        )
    )
    return MixPlan(duration_s=duration, profile=profile, bed=bed, sfx=clamped_sfx)


#: The mastering feel that best fits each mood (Phase 8). Tense/sombre scenes read
#: better speech-forward (clarity under tension); a triumphant beat wants punch; the
#: rest sit in the balanced cinematic default.
_MOOD_PRESET: dict[Mood, MasterPreset] = {
    Mood.TENSE: MasterPreset.DIALOGUE_FORWARD,
    Mood.SOMBRE: MasterPreset.DIALOGUE_FORWARD,
    Mood.TRIUMPHANT: MasterPreset.PUNCHY,
    Mood.PLAYFUL: MasterPreset.PUNCHY,
    Mood.CALM: MasterPreset.QUIET_ROOM,
    Mood.TENDER: MasterPreset.QUIET_ROOM,
}


def recommend_profile(mood: Mood, *, default: MasterPreset = MasterPreset.CINEMATIC) -> MixProfile:
    """The :class:`MixProfile` that best matches a scene's mood (Phase 8).

    A deterministic mood→preset table; moods without a strong preference fall to the
    balanced ``default`` (cinematic). Used by the render pipeline to master each
    scene in the feel its mood implies rather than one global preset.
    """
    return MixProfile.for_preset(_MOOD_PRESET.get(mood, default))


#: Default SFX accents per mood label — a small, tasteful set, applied opt-in.
_MOOD_SFX: dict[str, SfxKind] = {
    "tense": SfxKind.RUMBLE,
    "wondrous": SfxKind.SPARKLE,
    "triumphant": SfxKind.IMPACT,
}


def default_scene_sfx(mood_value: str, *, duration_s: float) -> list[SfxEvent]:
    """A single tasteful accent for a mood, or empty for moods that need none.

    Deterministic: the accent (when any) sits a beat into the scene so it colours
    the opening without stepping on the first words.
    """
    kind = _MOOD_SFX.get(mood_value)
    if kind is None or duration_s <= 0.0:
        return []
    return [SfxEvent(at_s=round(min(0.6, duration_s * 0.1), 3), kind=kind, gain_db=-12.0)]


__all__ = [
    "LocalCueLibrary",
    "MasterPreset",
    "MixPlan",
    "MixProfile",
    "MusicBedSpec",
    "MusicProvider",
    "SfxEvent",
    "SfxKind",
    "default_scene_sfx",
    "plan_mix",
    "recommend_profile",
]
