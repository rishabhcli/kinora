"""The Agent-03 film contract: wire models + pure builders (no DB / network).

This is the authoritative shape of the **event/scene film** HTTP responses, the
**sync map** (CONTRACTS.md §Agent-03), and the ``event_stitched`` /
``scene_stitched`` SSE payloads (§5.6). It mirrors — but does not import —
Agent 1's ``app.render.sync_map`` / ``app.render.stitch`` models, so this module
is self-contained and stays green while ``render/`` churns in parallel.

Key shapes:

* :class:`FilmSyncSegment` — one shot's window on the *film timeline*. The
  mission-core fields ``{shot_id, scene_id, word_range, t_start_s, t_end_s}`` are
  always present; ``{page, page_turn_at_s, words}`` are the §9.4 enrichment.
  ``t_start_s``/``t_end_s`` are the canonical names for render's
  ``video_start_s``/``video_end_s``.
* :func:`merge_and_build_film_sync_map` — fold Agent 1's *per-shot* (0-based)
  segments into one scene/event map with cumulative timestamps (§9.6) and the
  canonical field names. Mirrors ``app.render.stitch.merge_sync_segments``.
* :func:`film_sync_map_from_merged` — convert an *already-merged* render scene
  map to the canonical shape without re-shifting (for Agent 1's SSE emit).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Wire models
# --------------------------------------------------------------------------- #


class SyncWord(BaseModel):
    """One narrated word: film-timeline timing + page geometry to highlight it (§9.4)."""

    model_config = ConfigDict(extra="forbid")

    word_index: int
    text: str
    t_start: float
    t_end: float
    bbox: list[float] | None = None


class FilmSyncSegment(BaseModel):
    """One shot's window on the film timeline (the mission element + §9.4 enrichment)."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    scene_id: str
    word_range: tuple[int, int]
    t_start_s: float
    t_end_s: float
    page: int = 0
    page_turn_at_s: float = 0.0
    words: list[SyncWord] = Field(default_factory=list)


class FilmSyncMap(BaseModel):
    """The ordered segments for one film (scene or event), film-timeline seconds."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    duration_s: float
    segments: list[FilmSyncSegment] = Field(default_factory=list)


class SceneRef(BaseModel):
    """Lightweight pointer to a scene composing an event (in ``EventFilm.scenes``)."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    scene_index: int
    word_range: tuple[int, int]
    stitched: bool
    duration_s: float | None = None


class SceneFilm(BaseModel):
    """One scene's film — the partial-load unit (``GET .../scenes/{id}/film``)."""

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    event_id: str
    book_id: str
    scene_index: int
    event_index: int
    page_start: int
    page_end: int
    word_range: tuple[int, int]
    stitched: bool
    oss_url: str | None = None
    url_expires_at: str | None = None
    duration_s: float | None = None
    shot_count: int = 0
    sync_map: FilmSyncMap


class EventFilm(BaseModel):
    """One event's film — the reader-facing continuous film (== scene 1:1 today)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_index: int
    book_id: str
    page_start: int
    page_end: int
    word_range: tuple[int, int]
    stitched: bool
    oss_url: str | None = None
    url_expires_at: str | None = None
    duration_s: float | None = None
    shot_count: int = 0
    sync_map: FilmSyncMap
    scenes: list[SceneRef] = Field(default_factory=list)


class RestoreState(BaseModel):
    """Open-book context for Agent 12 to restore a reading session (§5.2)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    focus_word: int
    current_event_index: int | None = None
    current_scene_id: str | None = None
    mode: str


class EventsResponse(BaseModel):
    """``GET /api/books/{book_id}/events`` — all events + open-book restore state."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    url_ttl_s: int
    events: list[EventFilm] = Field(default_factory=list)
    restore: RestoreState | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_mapping(obj: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    """Normalize a render segment/word (dict or pydantic model) to a mapping."""
    if isinstance(obj, Mapping):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="python")  # type: ignore[no-any-return]
    raise TypeError(f"cannot read sync segment from {type(obj)!r}")


def _bbox(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def _word_range(spans: Mapping[str, Sequence[int]], shot_id: str) -> tuple[int, int]:
    wr = spans.get(shot_id)
    if wr is not None and len(wr) >= 2:
        return (int(wr[0]), int(wr[1]))
    return (0, 0)


def _shift_word(raw: Mapping[str, Any] | Any, shift: float) -> SyncWord:
    w = _as_mapping(raw)
    return SyncWord(
        word_index=int(w.get("word_index", 0)),
        text=str(w.get("text", "")),
        t_start=round(float(w.get("t_start", 0.0)) + shift, 3),
        t_end=round(float(w.get("t_end", 0.0)) + shift, 3),
        bbox=_bbox(w.get("bbox")),
    )


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def merge_and_build_film_sync_map(
    segments: Sequence[Mapping[str, Any] | Any],
    *,
    scene_id: str,
    spans: Mapping[str, Sequence[int]],
    durations: Sequence[float] | None = None,
) -> FilmSyncMap:
    """Fold *per-shot* (0-based) segments into one cumulative film map (§9.6).

    Each shot's segment is shifted onto the film timeline by the summed durations
    of the shots before it (mirrors ``app.render.stitch.merge_sync_segments``),
    then projected to the canonical :class:`FilmSyncSegment` shape — adding
    ``scene_id`` and the shot's ``word_range`` (from ``spans[shot_id]``) and
    renaming ``video_start_s``/``video_end_s`` to ``t_start_s``/``t_end_s``.

    ``durations`` (e.g. probed clip lengths) overrides each segment's own
    ``video_end_s - video_start_s`` when the concatenated length is known.
    """
    out: list[FilmSyncSegment] = []
    offset = 0.0
    for i, raw in enumerate(segments):
        seg = _as_mapping(raw)
        v_start = float(seg.get("video_start_s", 0.0))
        v_end = float(seg.get("video_end_s", 0.0))
        local_dur = (
            float(durations[i])
            if durations is not None and i < len(durations)
            else v_end - v_start
        )
        local_dur = max(0.0, local_dur)
        shift = offset - v_start
        shot_id = str(seg.get("shot_id", ""))
        out.append(
            FilmSyncSegment(
                shot_id=shot_id,
                scene_id=scene_id,
                word_range=_word_range(spans, shot_id),
                t_start_s=round(offset, 3),
                t_end_s=round(offset + local_dur, 3),
                page=int(seg.get("page", 0)),
                page_turn_at_s=round(float(seg.get("page_turn_at_s", 0.0)) + shift, 3),
                words=[_shift_word(w, shift) for w in seg.get("words", []) or []],
            )
        )
        offset += local_dur
    return FilmSyncMap(scene_id=scene_id, duration_s=round(offset, 3), segments=out)


def film_sync_map_from_merged(
    merged: Mapping[str, Any] | Any,
    *,
    scene_id: str,
    spans: Mapping[str, Sequence[int]],
) -> FilmSyncMap:
    """Convert an *already-merged* render ``SceneSyncMap`` to the canonical shape.

    No re-shift: the segments' ``video_start_s`` are already cumulative. Use this
    to emit canonical SSE from Agent 1's :class:`~app.render.stitch.StitchResult`.
    """
    m = _as_mapping(merged)
    out: list[FilmSyncSegment] = []
    for raw in m.get("segments", []) or []:
        seg = _as_mapping(raw)
        shot_id = str(seg.get("shot_id", ""))
        out.append(
            FilmSyncSegment(
                shot_id=shot_id,
                scene_id=scene_id,
                word_range=_word_range(spans, shot_id),
                t_start_s=round(float(seg.get("video_start_s", 0.0)), 3),
                t_end_s=round(float(seg.get("video_end_s", 0.0)), 3),
                page=int(seg.get("page", 0)),
                page_turn_at_s=round(float(seg.get("page_turn_at_s", 0.0)), 3),
                words=[_shift_word(w, 0.0) for w in seg.get("words", []) or []],
            )
        )
    return FilmSyncMap(
        scene_id=scene_id, duration_s=round(float(m.get("duration_s", 0.0)), 3), segments=out
    )


def scene_stitched_event(*, scene_id: str, oss_url: str, sync_map: FilmSyncMap) -> dict[str, Any]:
    """The §5.6 ``scene_stitched`` SSE payload (replace per-shot playback)."""
    return {
        "event": "scene_stitched",
        "scene_id": scene_id,
        "oss_url": oss_url,
        "sync_map": sync_map.model_dump(mode="json"),
    }


def event_stitched_event(*, event_id: str, oss_url: str, sync_map: FilmSyncMap) -> dict[str, Any]:
    """The ``event_stitched`` SSE payload (event-level rollup; event == scene today)."""
    return {
        "event": "event_stitched",
        "event_id": event_id,
        "oss_url": oss_url,
        "sync_map": sync_map.model_dump(mode="json"),
    }


__all__ = [
    "EventFilm",
    "EventsResponse",
    "FilmSyncMap",
    "FilmSyncSegment",
    "RestoreState",
    "SceneFilm",
    "SceneRef",
    "SyncWord",
    "event_stitched_event",
    "film_sync_map_from_merged",
    "merge_and_build_film_sync_map",
    "scene_stitched_event",
]
