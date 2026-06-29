"""Wire models for the two Kinora streams the pipelines consume.

These mirror the signals kinora.md already defines, so the stream processor
plugs straight onto the real event channel (§5.6) and reading-position model
(§4.3) without a translation layer:

* :class:`ReaderIntentEvent` — the *client → backend* intent signal. The reader's
  ``focus_word`` ``w`` (§4.3), instantaneous ``velocity_wps`` ``v``, ``mode``
  (viewer / director), and a ``seek`` flag for the §4.8 jump handler. Every event
  carries the session/book it belongs to and an explicit ``ts_ms`` (the client
  ``last_activity_ms``), which is the *event time* the watermark strategy reads.

* :class:`RenderEvent` — the *backend → client* generation events of §5.6:
  ``keyframe_ready`` / ``clip_ready`` / ``scene_stitched`` / ``regen_done`` /
  ``budget_low`` plus the QA verdict the Critic attaches (§9.5). It also carries
  the matching ``request_id`` so a *render request* can be interval-joined to its
  *clip-ready* for a true end-to-end latency.

Pure pydantic value objects — no I/O. Field names match the wire forms in §4.9 /
§5.6 so a producer can emit these verbatim.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class ReaderMode(enum.StrEnum):
    """§4.3 reading mode: who is driving the playhead."""

    VIEWER = "viewer"  # video drives
    DIRECTOR = "director"  # reader drives


class IntentKind(enum.StrEnum):
    """The kind of reader-intent signal (drives engagement classification)."""

    SETTLE = "settle"  # a debounced scroll-settle position update (§4.7)
    SCROLL = "scroll"  # raw forward motion sample
    SEEK = "seek"  # a far jump / scrub (§4.8)
    DWELL = "dwell"  # reader holding on a position
    IDLE = "idle"  # idle-pause fired (§4.7)


class ReaderIntentEvent(BaseModel):
    """One reader-intent signal on the §4.3 reading-position stream."""

    session_id: str
    book_id: str
    kind: IntentKind = IntentKind.SETTLE
    focus_word: int = Field(ge=0, description="word index w nearest the reading line (§4.3)")
    velocity_wps: float = Field(ge=0.0, description="instantaneous reading velocity v (words/s)")
    mode: ReaderMode = ReaderMode.VIEWER
    ts_ms: int = Field(description="client event time (epoch ms), == last_activity_ms")

    @property
    def is_forward(self) -> bool:
        """A positive-velocity, non-seek sample counts as forward reading."""

        return self.velocity_wps > 0.0 and self.kind in (
            IntentKind.SETTLE,
            IntentKind.SCROLL,
            IntentKind.DWELL,
        )


class RenderEventKind(enum.StrEnum):
    """§5.6 generation-event kinds plus the explicit render request marker."""

    RENDER_REQUESTED = "render_requested"  # Scheduler enqueued a shot (§4.9)
    KEYFRAME_READY = "keyframe_ready"
    CLIP_READY = "clip_ready"
    SCENE_STITCHED = "scene_stitched"
    REGEN_DONE = "regen_done"
    BUDGET_LOW = "budget_low"


class QAVerdict(enum.StrEnum):
    """The Critic's §9.5 verdict attached to a finished shot."""

    PASS = "pass"
    FAIL = "fail"  # rejected -> regenerate (counts toward regen rate, §13)
    DEGRADED = "degraded"  # dropped to the Ken-Burns ladder (§12.4)
    NONE = "none"  # not a QA-bearing event


class RenderEvent(BaseModel):
    """One §5.6 generation event on the render-event stream."""

    session_id: str
    book_id: str
    kind: RenderEventKind
    shot_id: str = ""
    request_id: str = Field(default="", description="links a request to its clip_ready")
    duration_s: float = Field(default=0.0, ge=0.0, description="video-seconds of this shot")
    ccs: float | None = Field(default=None, description="character-consistency score (§13)")
    qa: QAVerdict = QAVerdict.NONE
    budget_remaining_s: float | None = None
    ts_ms: int = Field(description="event time (epoch ms)")

    @property
    def is_accepted_clip(self) -> bool:
        """A clip that shipped (passed QA or rode the ladder) earns its seconds."""

        return self.kind == RenderEventKind.CLIP_READY and self.qa in (
            QAVerdict.PASS,
            QAVerdict.DEGRADED,
        )

    @property
    def is_regeneration(self) -> bool:
        """A QA failure or an explicit regen completion is a regeneration (§13)."""

        return self.kind == RenderEventKind.REGEN_DONE or self.qa == QAVerdict.FAIL
