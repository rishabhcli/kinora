"""Stitch + ship — concat accepted shots, merge the scene sync map (§9.6).

A scene is the stitch boundary (§4.2). Once its shots are accepted, this module:

* **concats** their clips into one scene mp4 with ffmpeg, re-encoding safely
  (each clip is first normalised to a common geometry / fps / audio layout so a
  degraded Ken-Burns rung and a full Wan clip concatenate cleanly), and
* **merges** the per-shot :class:`~app.render.sync_map.SyncSegment` s into a
  single scene-level sync map, shifting every video-time / word-time /
  page-turn by the cumulative offset of the preceding shots so the karaoke +
  page-turn stay correct across the whole scene (§9.4, §9.6).

The pure helpers — :func:`concat_clips` and :func:`merge_sync_segments` — take
raw bytes / segments and need no database, so the concat and the cumulative
timestamp math are unit-testable against real degrade-produced mp4s.
:class:`SceneStitcher` is the thin DB-backed orchestrator on top.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.beat import Beat
from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.scene import SceneRepo
from app.render.degrade import (
    DEFAULT_FPS,
    FILM_SIZE,
    FfmpegError,
    get_ffmpeg_exe,
    inspect,
    run_ffmpeg,
)
from app.render.sync_map import SyncSegment
from app.storage.object_store import ObjectStore, keys

logger = get_logger("app.render.stitch")

_AUDIO_SR = 44100


class SceneSyncMap(BaseModel):
    """The merged scene-level sync map (§9.4 scene shape)."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    duration_s: float
    segments: list[SyncSegment] = Field(default_factory=list)


class StitchResult(BaseModel):
    """The output of stitching a scene: the clip key/url + merged sync map."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    clip_key: str
    clip_url: str | None = None
    sync_map: SceneSyncMap
    duration_s: float
    shot_count: int


# --------------------------------------------------------------------------- #
# Pure: cumulative sync-map merge (§9.6)
# --------------------------------------------------------------------------- #


def _as_segment(seg: SyncSegment | Mapping[str, Any]) -> SyncSegment:
    return seg if isinstance(seg, SyncSegment) else SyncSegment.model_validate(seg)


def merge_sync_segments(
    segments: Sequence[SyncSegment | Mapping[str, Any]],
    *,
    scene_id: str,
    durations: Sequence[float] | None = None,
    overlap_s: float = 0.0,
) -> SceneSyncMap:
    """Merge per-shot segments into one scene map with cumulative timestamps.

    Each shot's segment is shifted onto the scene timeline by the summed
    durations of the shots before it. ``durations`` (e.g. probed clip lengths)
    overrides each segment's own ``video_end_s - video_start_s`` when the
    concatenated length is known precisely.

    ``overlap_s`` is the crossfade applied at each seam (§9.6 cinematic stitch):
    every shot after the first overlaps the prior shot's tail by that much, so it
    starts ``overlap_s`` *earlier* and the whole timeline shrinks by
    ``(n-1)·overlap_s`` — exactly matching the xfade'd clip the client plays.
    """
    merged: list[SyncSegment] = []
    start = 0.0
    for i, raw in enumerate(segments):
        seg = _as_segment(raw)
        local_dur = (
            durations[i]
            if durations is not None and i < len(durations)
            else seg.video_end_s - seg.video_start_s
        )
        shift = start - seg.video_start_s
        end = start + local_dur
        merged.append(
            SyncSegment(
                shot_id=seg.shot_id,
                video_start_s=round(start, 3),
                video_end_s=round(end, 3),
                page=seg.page,
                page_turn_at_s=round(seg.page_turn_at_s + shift, 3),
                words=[
                    word.model_copy(
                        update={
                            "t_start": round(word.t_start + shift, 3),
                            "t_end": round(word.t_end + shift, 3),
                        }
                    )
                    for word in seg.words
                ],
            )
        )
        # The next shot crossfades into this one's tail (overlap), so it begins early.
        start = end - overlap_s
    duration = round(merged[-1].video_end_s, 3) if merged else 0.0
    return SceneSyncMap(scene_id=scene_id, duration_s=duration, segments=merged)


# --------------------------------------------------------------------------- #
# Pure: ffmpeg concat (re-encode safely)
# --------------------------------------------------------------------------- #


def _safe_has_audio(clip: bytes) -> bool:
    try:
        return inspect(clip).has_audio
    except FfmpegError:
        return False


def _safe_duration(clip: bytes) -> float:
    try:
        return inspect(clip).duration_s
    except FfmpegError:
        return 0.0


def effective_crossfade(durations: Sequence[float], requested_s: float) -> float:
    """Clamp a requested crossfade so it never swallows a whole (short) shot.

    A crossfade longer than ~half the shortest clip would push the xfade offset
    negative / leave no clean frames, so the request is capped at 45 % of the
    shortest clip. Returns 0 when there is nothing to fade (one clip / no request).
    """
    if requested_s <= 0:
        return 0.0
    positive = [d for d in durations if d > 0]
    if len(positive) < 2:
        return 0.0
    return round(min(requested_s, 0.45 * min(positive)), 3)


def _xfade_concat(
    normalized: Sequence[bytes],
    durs: Sequence[float],
    *,
    size: tuple[int, int],
    fps: int,
    crossfade_s: float,
) -> bytes:
    """Concatenate with a video xfade + audio acrossfade at every seam (§9.6).

    Each clip dissolves into the next over ``crossfade_s`` (so the film is one
    continuous event with no hard cuts / black frames), the audio crossfades in
    lockstep, and the final mix is level-normalised (``dynaudnorm``) so a quiet
    clip never jumps a loud one. The total length is the sum of the clips minus
    the ``(n-1)`` overlaps — the same timeline :func:`merge_sync_segments` builds.

    ``durs`` are the probed (positive) clip durations the caller already measured,
    used to advance each xfade offset.
    """
    ffmpeg = get_ffmpeg_exe()
    n = len(normalized)
    with tempfile.TemporaryDirectory(prefix="kinora_xfade_") as tmp:
        tmp_dir = Path(tmp)
        args: list[str] = [ffmpeg, "-y"]
        for i, clip in enumerate(normalized):
            seg_path = tmp_dir / f"seg_{i}.mp4"
            seg_path.write_bytes(clip)
            args += ["-i", str(seg_path)]

        parts: list[str] = []
        # Video: chain pairwise xfades, advancing the offset by the running length.
        prev_v = "[0:v]"
        running = durs[0]
        for k in range(1, n):
            offset = max(0.0, running - crossfade_s)
            out_v = "[vout]" if k == n - 1 else f"[vx{k}]"
            parts.append(
                f"{prev_v}[{k}:v]xfade=transition=fade:"
                f"duration={crossfade_s:.3f}:offset={offset:.3f}{out_v}"
            )
            prev_v = out_v
            running = running + durs[k] - crossfade_s
        # Audio: matching acrossfades, then a single loudness normalisation pass.
        prev_a = "[0:a]"
        for k in range(1, n):
            out_a = "[araw]" if k == n - 1 else f"[ax{k}]"
            parts.append(f"{prev_a}[{k}:a]acrossfade=d={crossfade_s:.3f}{out_a}")
            prev_a = out_a
        parts.append("[araw]dynaudnorm[aout]")

        out_path = tmp_dir / "scene.mp4"
        args += [
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-ar",
            str(_AUDIO_SR),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        run_ffmpeg(args)
        scene = out_path.read_bytes()
    logger.info(
        "stitch.xfade",
        clips=n,
        bytes=len(scene),
        size=f"{size[0]}x{size[1]}",
        crossfade_s=round(crossfade_s, 3),
    )
    return scene


def _normalize_segment(clip: bytes, *, size: tuple[int, int], fps: int) -> bytes:
    """Re-encode one clip to a common geometry/fps + stereo AAC (silence if mute).

    Uniform parameters are what let the concat filter join a degraded Ken-Burns
    rung and a full Wan clip without artefacts.
    """
    ffmpeg = get_ffmpeg_exe()
    out_w, out_h = size
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    )
    has_audio = _safe_has_audio(clip)
    duration = 0.0
    try:
        duration = inspect(clip).duration_s
    except FfmpegError:
        duration = 0.0

    with tempfile.TemporaryDirectory(prefix="kinora_norm_") as tmp:
        tmp_dir = Path(tmp)
        in_path = tmp_dir / "in.mp4"
        in_path.write_bytes(clip)
        out_path = tmp_dir / "norm.mp4"
        args = [ffmpeg, "-y", "-i", str(in_path)]
        if has_audio:
            args += ["-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "0:a:0"]
        else:
            args += [
                "-f",
                "lavfi",
                "-t",
                f"{max(duration, 0.1):.3f}",
                "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_SR}",
                "-filter_complex",
                f"[0:v]{vf}[v]",
                "-map",
                "[v]",
                "-map",
                "1:a:0",
                "-shortest",
            ]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-ar",
            str(_AUDIO_SR),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        run_ffmpeg(args)
        return out_path.read_bytes()


def concat_clips(
    clips: Sequence[bytes],
    *,
    size: tuple[int, int] | None = None,
    fps: int = DEFAULT_FPS,
    crossfade_s: float = 0.0,
) -> bytes:
    """Concatenate clips into one mp4, normalising then re-encoding (§9.6).

    Args:
        clips: the ordered clip byte-strings (full Wan and/or degraded rungs).
        size: output geometry; defaults to the vertical :data:`FILM_SIZE`
            (720×1280) so the stitch *enforces* the film aspect rather than
            inheriting whatever the first clip happened to be — a landscape or
            mismatched source is scaled+padded into vertical, never leaked.
        fps: output frame rate.
        crossfade_s: when > 0, dissolve each seam with a video xfade + audio
            acrossfade of this length (clamped to 45 % of the shortest clip) for a
            continuous event film with no hard cuts; 0 is a straight (hard-cut)
            concat. Either way the audio is level-normalised.

    Raises:
        ValueError: when ``clips`` is empty.
        FfmpegError: when no ffmpeg binary is available or a step fails.
    """
    if not clips:
        raise ValueError("concat_clips requires at least one clip")
    out_size = size or FILM_SIZE
    normalized = [_normalize_segment(clip, size=out_size, fps=fps) for clip in clips]
    if len(normalized) == 1:
        return normalized[0]

    durs = [_safe_duration(c) for c in normalized]
    crossfade = effective_crossfade(durs, crossfade_s)
    # Only dissolve when every clip's duration probed cleanly; a 0 (probe failure)
    # would mis-place the xfade offsets, so fall back to the robust hard concat.
    if crossfade > 0 and all(d > 0 for d in durs):
        return _xfade_concat(normalized, durs, size=out_size, fps=fps, crossfade_s=crossfade)

    ffmpeg = get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="kinora_concat_") as tmp:
        tmp_dir = Path(tmp)
        args: list[str] = [ffmpeg, "-y"]
        for i, clip in enumerate(normalized):
            seg_path = tmp_dir / f"seg_{i}.mp4"
            seg_path.write_bytes(clip)
            args += ["-i", str(seg_path)]
        streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(normalized)))
        # Level-normalise the concatenated audio so a quiet clip never jumps a loud one.
        graph = f"{streams}concat=n={len(normalized)}:v=1:a=1[v][araw];[araw]dynaudnorm[a]"
        out_path = tmp_dir / "scene.mp4"
        args += [
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-ar",
            str(_AUDIO_SR),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        run_ffmpeg(args)
        scene = out_path.read_bytes()
    logger.info(
        "stitch.concat",
        clips=len(normalized),
        bytes=len(scene),
        size=f"{out_size[0]}x{out_size[1]}",
    )
    return scene


# --------------------------------------------------------------------------- #
# DB-backed orchestrator
# --------------------------------------------------------------------------- #


class SceneStitcher:
    """Stitch a scene's accepted shots into one clip + merged sync map (§9.6)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        object_store: ObjectStore,
        url_ttl: int = 3600,
        fps: int = DEFAULT_FPS,
    ) -> None:
        self._session = session
        self._scenes = SceneRepo(session)
        self._store = object_store
        self._ttl = url_ttl
        self._fps = fps

    async def stitch_scene(self, scene_id: str) -> StitchResult:
        """Fetch accepted shots in order, concat, merge sync maps, upload, return.

        Raises:
            LookupError: when the scene is unknown.
            ValueError: when the scene has no accepted, clip-bearing shots.
        """
        scene = await self._scenes.get(scene_id)
        if scene is None:
            raise LookupError(f"unknown scene_id: {scene_id}")

        shots = await self._accepted_shots_in_order(scene_id)
        clips: list[bytes] = []
        segments: list[SyncSegment] = []
        durations: list[float] = []
        for shot in shots:
            clip_key = (shot.output or {}).get("clip_key")
            if not clip_key:
                continue
            clip_bytes = await anyio.to_thread.run_sync(self._store.get_bytes, clip_key)
            clips.append(clip_bytes)
            segments.append(self._segment_for(shot, clip_bytes))
            durations.append(self._duration_for(shot, clip_bytes))

        if not clips:
            raise ValueError(f"scene {scene_id} has no accepted shots with clips to stitch")

        scene_clip = await anyio.to_thread.run_sync(lambda: concat_clips(clips, fps=self._fps))
        sync_map = merge_sync_segments(segments, scene_id=scene_id, durations=durations)

        clip_key = keys.clip(scene.book_id, scene_id)
        await anyio.to_thread.run_sync(self._store.put_bytes, clip_key, scene_clip, "video/mp4")
        clip_url = await anyio.to_thread.run_sync(
            lambda: self._store.presigned_get_url(clip_key, ttl=self._ttl)
        )
        logger.info(
            "stitch.scene",
            scene_id=scene_id,
            shots=len(clips),
            duration_s=sync_map.duration_s,
            clip_key=clip_key,
        )
        return StitchResult(
            scene_id=scene_id,
            clip_key=clip_key,
            clip_url=clip_url,
            sync_map=sync_map,
            duration_s=sync_map.duration_s,
            shot_count=len(clips),
        )

    async def _accepted_shots_in_order(self, scene_id: str) -> list[Shot]:
        """Accepted shots for a scene in narrative order (by beat ordinal)."""
        stmt = (
            select(Shot)
            .join(Beat, Beat.id == Shot.beat_id)
            .where(Shot.scene_id == scene_id, Shot.status == ShotStatus.ACCEPTED)
            .order_by(Beat.beat_index)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def _segment_for(self, shot: Shot, clip_bytes: bytes) -> SyncSegment:
        raw = (shot.narration or {}).get("sync_segment")
        if isinstance(raw, Mapping):
            return SyncSegment.model_validate(raw)
        # No stored segment: a minimal one spanning the clip's measured length.
        return SyncSegment(
            shot_id=shot.id,
            video_start_s=0.0,
            video_end_s=self._duration_for(shot, clip_bytes),
            page=int((shot.source_span or {}).get("page", 0)),
            page_turn_at_s=max(0.0, self._duration_for(shot, clip_bytes) - 0.2),
            words=[],
        )

    @staticmethod
    def _duration_for(shot: Shot, clip_bytes: bytes) -> float:
        if shot.duration_s:
            return float(shot.duration_s)
        try:
            return inspect(clip_bytes).duration_s
        except FfmpegError:
            return 0.0


__all__ = [
    "SceneStitcher",
    "SceneSyncMap",
    "StitchResult",
    "concat_clips",
    "effective_crossfade",
    "merge_sync_segments",
]
