"""Phase B — the per-shot render pipeline (kinora.md §9.2–§9.7, §12.4).

This package turns a promoted shot into a playable, page-synced segment:

* :mod:`app.render.states` — the §9.7 per-shot state machine (validated edges +
  a persist hook);
* :mod:`app.render.sync_map` — the §9.4 sync-map builder (narration word timings
  aligned to page word boxes → karaoke + page-turn), pure and unit-testable;
* :mod:`app.render.degrade` — the §4.4/§12.4 degradation ladder as **real
  ffmpeg**: a Ken-Burns pan over a keyframe / book illustration, muxed with
  narration — a genuine product rung, not a fake fallback;
* :mod:`app.render.conflict` — the §7.2 arbitration wiring (Critic → Continuity
  → Showrunner → apply honor/evolve/surface);
* :mod:`app.render.stitch` — §9.6 scene concat + cumulative sync-map merge;
* :mod:`app.render.pipeline` — the per-shot orchestrator (§9.2): cache → design
  → budget-gated live render → Critic repair loop (§9.5) → degradation ladder.
"""

from __future__ import annotations

from app.render.conflict import ConflictResolution, ConflictResolver
from app.render.degrade import (
    DegradeRung,
    FfmpegError,
    ProbeInfo,
    audio_text_card,
    ffmpeg_available,
    ken_burns_over_image,
    probe,
    verify_playable,
)
from app.render.pipeline import (
    RenderPipeline,
    RenderResult,
    UnknownShotError,
    build_render_pipeline,
)
from app.render.states import (
    RenderState,
    ShotStateMachine,
    is_allowed,
    to_status,
)
from app.render.stitch import (
    SceneStitcher,
    SceneSyncMap,
    StitchResult,
    concat_clips,
    merge_sync_segments,
)
from app.render.sync_map import (
    SyncSegment,
    SyncWord,
    align_words,
    build_sync_segment,
)

__all__ = [
    "ConflictResolution",
    "ConflictResolver",
    "DegradeRung",
    "FfmpegError",
    "ProbeInfo",
    "RenderPipeline",
    "RenderResult",
    "RenderState",
    "SceneStitcher",
    "SceneSyncMap",
    "ShotStateMachine",
    "StitchResult",
    "SyncSegment",
    "SyncWord",
    "UnknownShotError",
    "align_words",
    "audio_text_card",
    "build_render_pipeline",
    "build_sync_segment",
    "concat_clips",
    "ffmpeg_available",
    "is_allowed",
    "ken_burns_over_image",
    "merge_sync_segments",
    "probe",
    "to_status",
    "verify_playable",
]
