"""Audio post-production — score the bed, place SFX, duck under speech, master (§9.6).

Turns a scene's raw narration into a **mixed, mastered** track: a procedurally
synthesised music bed (from a :class:`~app.providers.audio.MixPlan`), optional timed
SFX, side-chain ducking so the bed drops under speech and swells in the gaps, and a
final EBU-R128 ``loudnorm`` so every scene plays at a consistent level. All real
ffmpeg, all deterministic, **zero model spend** — the bed/SFX are synthesised from
ffmpeg's own ``sine`` / ``anoisesrc`` sources, so nothing is downloaded and nothing
is copyrighted.

Split, exactly like :mod:`app.render.stitch`, into:

* **pure filter builders** (:func:`bed_filter`, :func:`sfx_filter`,
  :func:`ducking_filter`, :func:`loudnorm_filter`) that emit ffmpeg filtergraph
  fragments from a plan — unit-tested with no ffmpeg, and
* :func:`master_scene_audio`, the thin orchestrator that runs ffmpeg and returns a
  real, playable WAV — tested against the bundled/system binary (skipped if absent).

The narration WAV is the side-chain key for ducking; when no narration is supplied
the bed is mastered alone (a wordless establishing scene still gets a scored bed).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.providers.audio import MixPlan, MixProfile, MusicBedSpec, SfxEvent, SfxKind
from app.render.degrade import FfmpegError, get_ffmpeg_exe, inspect, run_ffmpeg
from app.render.music import ScoreCue

logger = get_logger("app.render.audio_post")

#: The scene audio is mastered at this rate (matches the stitch path's 44.1k).
MASTER_SAMPLE_RATE = 44100


@dataclass(frozen=True, slots=True)
class SceneAudioResult:
    """A mastered scene audio track + a record of which stages were applied."""

    audio_bytes: bytes
    sample_rate: int
    duration_s: float
    #: The mix stages that ran, e.g. ``["bed", "sfx", "duck", "loudnorm"]`` — useful
    #: telemetry and lets a caller assert the master did what the plan asked.
    applied: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Pure filtergraph builders
# --------------------------------------------------------------------------- #


def _fnum(value: float) -> str:
    """Format a float for an ffmpeg filter arg (trim trailing zeros, keep a digit)."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def bed_filter(bed: MusicBedSpec, *, sr: int = MASTER_SAMPLE_RATE) -> str:
    """The filtergraph that synthesises the music bed from its :class:`ScoreCue`.

    Stacks the cue's (root, third, fifth) as detuned sine drones, sums them, shapes
    the level with a slow tremolo at the cue tempo (the "breathing" pad), applies the
    bed gain + head/tail fades, and stereo-izes. Pure string assembly — no ffmpeg
    runs here. The output label is ``[bed]``.

    Inputs are ``sine`` lavfi sources appended by :func:`master_scene_audio`; this
    builder assumes three consecutive input labels ``[r]``, ``[t]``, ``[f]`` already
    scaled+named by the caller's input setup. To keep the builder self-contained we
    instead reference them positionally via ``amix`` of the three named inputs.
    """
    cue = bed.cue
    # Tremolo (the pad swell) breathes at the cue tempo: cycles/sec = bpm/60.
    tremolo_hz = max(0.05, cue.tempo_bpm / 60.0 / 4.0)
    gain = _fnum(_db_to_amp(bed.gain_db))
    fade_in = _fnum(bed.fade_in_s)
    fade_out = _fnum(bed.fade_out_s)
    fade_out_start = _fnum(max(0.0, bed.duration_s - bed.fade_out_s))
    return (
        "[r][t][f]amix=inputs=3:normalize=1[chord];"
        f"[chord]tremolo=f={_fnum(tremolo_hz)}:d=0.3,"
        f"volume={gain},"
        f"afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={fade_out_start}:d={fade_out},"
        f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[bed]"
    )


def sfx_filter(event: SfxEvent, index: int, *, sr: int = MASTER_SAMPLE_RATE) -> str:
    """The filtergraph for one synth SFX transient, delayed onto the timeline.

    Each :class:`SfxKind` is a short shaped synth: a sweep, a low thud, a bright
    shimmer, a noise rumble, or a soft chime. The transient is gained, fast-faded so
    it doesn't click, delayed to ``event.at_s``, and named ``[sfx{index}]``. Assumes
    the caller appended an appropriate lavfi source labelled ``[sfxsrc{index}]``.
    """
    delay_ms = int(round(event.at_s * 1000))
    gain = _fnum(_db_to_amp(event.gain_db))
    return (
        f"[sfxsrc{index}]volume={gain},"
        f"afade=t=in:st=0:d=0.02,afade=t=out:st={_fnum(max(0.0, event.duration_s - 0.15))}:d=0.15,"
        f"adelay={delay_ms}|{delay_ms},"
        f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[sfx{index}]"
    )


def ducking_filter(profile: MixProfile, *, bed_label: str, key_label: str, out_label: str) -> str:
    """Side-chain compress the bed (``bed_label``) against speech (``key_label``).

    The bed drops by the profile's ratio whenever speech crosses the threshold and
    swells back over the release — the standard dialogue-ducking move. Emits a
    ``sidechaincompress`` fragment writing ``out_label``. Pure string assembly.
    """
    return (
        f"[{bed_label}][{key_label}]sidechaincompress="
        f"threshold={_fnum(_db_to_amp(profile.duck_threshold_db))}:"
        f"ratio={_fnum(profile.duck_ratio)}:"
        f"attack={_fnum(profile.duck_attack_ms)}:"
        f"release={_fnum(profile.duck_release_ms)}[{out_label}]"
    )


def loudnorm_filter(profile: MixProfile) -> str:
    """The EBU-R128 ``loudnorm`` fragment for the profile's mastering target.

    Single-pass loudnorm to ``target_lufs`` with the profile's true-peak ceiling and
    target loudness range — the consistent-level master every scene ends on.
    """
    return (
        f"loudnorm=I={_fnum(profile.target_lufs)}:"
        f"TP={_fnum(profile.true_peak_db)}:"
        f"LRA={_fnum(profile.loudness_range)}"
    )


def _db_to_amp(db: float) -> float:
    """Convert a dB gain to a linear amplitude for ffmpeg ``volume=``."""
    return round(10.0 ** (db / 20.0), 6)


# --------------------------------------------------------------------------- #
# SFX source descriptors (lavfi inputs the synth transients are built from)
# --------------------------------------------------------------------------- #

#: Each SFX kind → the lavfi source expression that seeds its transient. Kept here
#: (not in the filter builder) so the input list and the filtergraph stay in lockstep.
def _sfx_source(kind: SfxKind, duration_s: float, *, sr: int) -> str:
    d = _fnum(max(0.05, duration_s))
    if kind is SfxKind.WHOOSH:
        # A rising noise sweep through a sweeping band-pass.
        return f"anoisesrc=d={d}:c=pink:r={sr},highpass=f=300,lowpass=f=6000"
    if kind is SfxKind.IMPACT:
        # A low decaying thud.
        return f"sine=frequency=70:duration={d}:sample_rate={sr}"
    if kind is SfxKind.SPARKLE:
        # A bright shimmer.
        return f"sine=frequency=1760:duration={d}:sample_rate={sr}"
    if kind is SfxKind.RUMBLE:
        return f"anoisesrc=d={d}:c=brown:r={sr},lowpass=f=120"
    # CHIME (default): a soft mid tone.
    return f"sine=frequency=880:duration={d}:sample_rate={sr}"


# --------------------------------------------------------------------------- #
# The orchestrator (real ffmpeg)
# --------------------------------------------------------------------------- #


def master_scene_audio(
    plan: MixPlan,
    *,
    narration_wav: bytes | None = None,
    sr: int = MASTER_SAMPLE_RATE,
) -> SceneAudioResult:
    """Render the scene's mixed, mastered audio as a real WAV (§9.6).

    Pipeline: synth bed ← cue → (optional SFX overlay) → duck under narration →
    sum with narration → ``loudnorm`` master. When ``narration_wav`` is ``None`` the
    bed (and SFX) are mastered alone. When the plan has no music (``intensity`` 0 or
    zero duration) and narration is present, the narration is mastered straight.

    Args:
        plan: the deterministic :class:`MixPlan` (from ``providers.audio.plan_mix``).
        narration_wav: the scene narration WAV (the ducking side-chain key).
        sr: master sample rate.

    Returns:
        A :class:`SceneAudioResult` whose ``applied`` records the stages that ran.

    Raises:
        FfmpegError: when no ffmpeg binary is available or a step fails.
        ValueError: when there is nothing to mix (no music and no narration).
    """
    if not plan.has_music and not narration_wav:
        raise ValueError("master_scene_audio: nothing to mix (no music bed, no narration)")

    ffmpeg = get_ffmpeg_exe()
    applied: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kinora_audio_") as tmp:
        tmp_dir = Path(tmp)
        args: list[str] = [ffmpeg, "-y"]
        parts: list[str] = []
        input_index = 0

        # -- narration input (the side-chain key + the speech to sum) ----------- #
        # The narration is the duck *key* only when a music bed will be ducked
        # against it; splitting it unconditionally would leave an unconnected
        # ``asplit`` output (an ffmpeg bind error). So tap it once or twice based
        # on whether the bed needs the side-chain.
        will_duck = bool(narration_wav) and plan.has_music
        key_label: str | None = None
        if narration_wav:
            nar_path = tmp_dir / "narration.wav"
            nar_path.write_bytes(narration_wav)
            args += ["-i", str(nar_path)]
            nar_idx = input_index
            input_index += 1
            fmt = (
                f"[{nar_idx}:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo"
            )
            if will_duck:
                parts.append(f"{fmt},asplit=2[speech][duckkey]")
                key_label = "duckkey"
            else:
                parts.append(f"{fmt}[speech]")

        bed_out: str | None = None
        if plan.has_music:
            # Three sine drones (root/third/fifth) as the chord inputs the bed mixes.
            for freq in plan.bed.cue.chord_hz:
                args += [
                    "-f", "lavfi", "-t", _fnum(plan.duration_s),
                    "-i", f"sine=frequency={_fnum(freq)}:sample_rate={sr}",
                ]
                input_index += 1
            # Re-label the three sine inputs into [r][t][f] for the bed builder.
            sine_base = input_index - 3
            parts.append(f"[{sine_base}:a]anull[r]")
            parts.append(f"[{sine_base + 1}:a]anull[t]")
            parts.append(f"[{sine_base + 2}:a]anull[f]")
            parts.append(bed_filter(plan.bed, sr=sr))
            applied.append("bed")
            bed_out = "bed"

            # -- SFX overlaid onto the bed ------------------------------------- #
            sfx_labels: list[str] = []
            for i, event in enumerate(plan.sfx):
                args += [
                    "-f", "lavfi",
                    "-i", _sfx_source(event.kind, event.duration_s, sr=sr),
                ]
                src_idx = input_index
                input_index += 1
                parts.append(f"[{src_idx}:a]anull[sfxsrc{i}]")
                parts.append(sfx_filter(event, i, sr=sr))
                sfx_labels.append(f"[sfx{i}]")
            if sfx_labels:
                bedmix_inputs = "[bed]" + "".join(sfx_labels)
                n = len(sfx_labels) + 1
                parts.append(f"{bedmix_inputs}amix=inputs={n}:normalize=0:duration=first[bedmix]")
                bed_out = "bedmix"
                applied.append("sfx")

            # -- duck the bed under speech ------------------------------------- #
            if key_label is not None:
                parts.append(
                    ducking_filter(
                        plan.profile, bed_label=bed_out, key_label=key_label, out_label="ducked"
                    )
                )
                bed_out = "ducked"
                applied.append("duck")

        # -- final sum + master ------------------------------------------------ #
        loud = loudnorm_filter(plan.profile)
        applied.append("loudnorm")
        if narration_wav and bed_out is not None:
            parts.append(f"[speech][{bed_out}]amix=inputs=2:normalize=0:duration=longest[mixed]")
            parts.append(f"[mixed]{loud}[out]")
        elif narration_wav:  # music-less: master the narration straight
            parts.append(f"[speech]{loud}[out]")
        else:  # bed-only scene
            parts.append(f"[{bed_out}]{loud}[out]")

        out_path = tmp_dir / "scene_audio.wav"
        args += [
            "-filter_complex", ";".join(parts),
            "-map", "[out]",
            "-ar", str(sr),
            "-ac", "2",
            "-c:a", "pcm_s16le",
        ]
        if plan.has_music and not narration_wav:
            # A bed-only scene has no intrinsic length cap — bound it to the plan.
            args += ["-t", _fnum(plan.duration_s)]
        args += [str(out_path)]
        run_ffmpeg(args)
        audio = out_path.read_bytes()

    duration = _safe_audio_duration(audio, fallback=plan.duration_s)
    logger.info(
        "audio_post.master",
        preset=plan.profile.preset,
        duration_s=round(duration, 3),
        applied=",".join(applied),
        bytes=len(audio),
    )
    return SceneAudioResult(
        audio_bytes=audio, sample_rate=sr, duration_s=duration, applied=tuple(applied)
    )


def _safe_audio_duration(audio: bytes, *, fallback: float) -> float:
    try:
        probed = inspect(audio).duration_s
    except FfmpegError:
        return fallback
    return probed if probed > 0 else fallback


def score_and_master(
    *,
    narration_wav: bytes | None,
    cue: ScoreCue,
    profile: MixProfile,
    duration_s: float,
    sfx: list[SfxEvent] | None = None,
    sr: int = MASTER_SAMPLE_RATE,
) -> SceneAudioResult:
    """Convenience: plan the mix then master it in one call (the §9.6 entry point).

    The render pipeline scores the scene (``music.score_scene``), then calls this to
    get the mastered track to mux under the stitched video.
    """
    from app.providers.audio import plan_mix

    plan = plan_mix(duration_s=duration_s, cue=cue, profile=profile, sfx=sfx)
    return master_scene_audio(plan, narration_wav=narration_wav, sr=sr)


def score_scene_to_audio(
    *,
    narration_wav: bytes | None,
    duration_s: float,
    mood_text: str | None = None,
    palette: str | None = None,
    with_sfx: bool = True,
    intensity_override: float | None = None,
    sr: int = MASTER_SAMPLE_RATE,
) -> SceneAudioResult:
    """The full §9.6 audio entry point: mood/palette text → mastered scene track.

    Classifies the scene mood, scores its cue (intensity nudged by palette / a learned
    ``intensity_override``), picks the mastering profile that fits the mood, adds a
    tasteful default SFX accent for high-arousal moods (``with_sfx``), and masters —
    ducking the bed under ``narration_wav`` when present. One call, fully
    deterministic, zero model spend.
    """
    from app.providers.audio import default_scene_sfx, recommend_profile
    from app.render.music import classify_mood, score_scene

    mood = classify_mood(mood_text)
    cue = score_scene(
        mood_text=mood_text, palette=palette, intensity_override=intensity_override
    )
    profile = recommend_profile(mood)
    sfx = default_scene_sfx(mood.value, duration_s=duration_s) if with_sfx else None
    return score_and_master(
        narration_wav=narration_wav,
        cue=cue,
        profile=profile,
        duration_s=duration_s,
        sfx=sfx,
        sr=sr,
    )


__all__ = [
    "MASTER_SAMPLE_RATE",
    "SceneAudioResult",
    "bed_filter",
    "ducking_filter",
    "loudnorm_filter",
    "master_scene_audio",
    "score_and_master",
    "score_scene_to_audio",
    "sfx_filter",
]
