"""The typed plan vocabulary the planner emits — request, steps, and cost.

A :class:`CanonicalVideoRequest` is what the caller *wants* (mode, duration, fps,
resolution, aspect, references, seed, prompt) expressed against the
provider-neutral :class:`~app.video.planning.capabilities.VideoMode`. The planner
turns it, plus a target :class:`~app.video.planning.capabilities.CapabilityProfile`,
into a :class:`RenderPlan`: an ordered list of concrete :class:`PlanStep` s (each a
provider call + an optional post-step), a human-readable ``rationale``, and a
``fidelity_cost`` score the router can compare across providers.

All models are pydantic v2 and pure data; the planner builds them, nothing here
talks to a network.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .capabilities import ASPECT_16_9, AspectRatio, VideoMode


class CanonicalVideoRequest(BaseModel):
    """The provider-neutral, *desired* video request before any negotiation.

    This is the "ideal" — what the creative plane asked for. The planner never
    mutates it; it derives a sequence of concrete provider calls that approximate
    it within a backend's declared envelope.

    Attributes:
        mode: The desired :class:`VideoMode`.
        prompt / negative_prompt: The text driving the render.
        duration_s: Total desired clip length (may exceed a backend's per-call max,
            in which case the plan chains segments).
        fps / resolution / aspect: Desired output format.
        seed: Desired deterministic seed (``None`` = don't care).
        reference_image_urls: r2v locked-appearance references.
        image_url: i2v / continuation single driving frame.
        first_frame_url / last_frame_url: first-last-frame endpoints.
        source_video_url: continuation / instruction-edit prior clip.
        shot_id: carried through for idempotency/telemetry.
    """

    model_config = ConfigDict(frozen=True)

    mode: VideoMode = VideoMode.TEXT_TO_VIDEO
    prompt: str = ""
    negative_prompt: str | None = None

    duration_s: float = 5.0
    fps: int = 24
    resolution: str = "720P"
    aspect: AspectRatio = ASPECT_16_9

    seed: int | None = None

    reference_image_urls: tuple[str, ...] = ()
    image_url: str | None = None
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    source_video_url: str | None = None

    shot_id: str | None = None


class StepKind(StrEnum):
    """What a :class:`PlanStep` is — a render call or a translation pre-step."""

    #: Synthesize a still keyframe (text→image) to feed a downstream i2v.
    SYNTHESIZE_KEYFRAME = "synthesize_keyframe"
    #: A concrete provider video render call.
    RENDER = "render"


class PostOp(StrEnum):
    """A downstream post-processing marker attached to a render step.

    These are *instructions for the render pipeline*, not actions the planner
    performs. The pipeline reads them to know it must upscale, retime, pad, stitch,
    or crop the produced asset to reach the canonical request.
    """

    NONE = "none"
    #: The produced resolution is below target; upscale downstream.
    UPSCALE = "upscale"
    #: The produced resolution is above target; downscale downstream.
    DOWNSCALE = "downscale"
    #: The produced fps differs from target; retime (interpolate/drop) downstream.
    RETIME_FPS = "retime_fps"
    #: The produced aspect differs from target; pad or crop to the canonical aspect.
    REFRAME_ASPECT = "reframe_aspect"
    #: This segment's tail overlaps the next segment's head; trim+crossfade on stitch.
    STITCH_OVERLAP = "stitch_overlap"
    #: This segment abuts the next with a hard cut; concatenate on stitch.
    STITCH_CUT = "stitch_cut"
    #: The last frame of this segment seeds the next continuation segment.
    EXTRACT_LAST_FRAME = "extract_last_frame"


class PlanStep(BaseModel):
    """One executable step: a concrete provider call plus post-step markers.

    A render step carries a *resolved* mode + format that the backend actually
    accepts (already clamped by the planner). ``post_ops`` tells the pipeline what
    correction to apply to this step's output to converge on the canonical request.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    kind: StepKind

    #: The backend-accepted mode for a RENDER step (``None`` for a keyframe synth).
    mode: VideoMode | None = None
    prompt: str = ""
    negative_prompt: str | None = None

    duration_s: float = 0.0
    fps: int = 0
    resolution: str = ""
    aspect: AspectRatio | None = None
    seed: int | None = None

    reference_image_urls: tuple[str, ...] = ()
    image_url: str | None = None
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    source_video_url: str | None = None

    #: For continuation chains: the segment ordinal (0-based) and total count.
    segment_index: int = 0
    segment_count: int = 1

    post_ops: tuple[PostOp, ...] = ()
    #: A short note on why this step exists / what it compensates for.
    note: str = ""


class FidelityPenalty(StrEnum):
    """Named, additive contributions to a plan's fidelity cost.

    Each is a category of "how far from the ideal" a translation pushes the
    result. The planner attaches one per concession with a weight; the sum is the
    plan's :attr:`RenderPlan.fidelity_cost`.
    """

    KEYFRAME_SYNTH = "keyframe_synth"  # r2v/flf → synth keyframe + i2v
    MODE_DOWNGRADE = "mode_downgrade"  # asked-for mode unavailable, substituted
    DURATION_SPLIT = "duration_split"  # chained into N segments
    DURATION_CLAMP = "duration_clamp"  # snapped to a shorter discrete duration
    FPS_CLAMP = "fps_clamp"
    RESOLUTION_CLAMP = "resolution_clamp"
    ASPECT_CLAMP = "aspect_clamp"
    SEED_DROPPED = "seed_dropped"  # non-determinism introduced
    PROMPT_COMPRESSED = "prompt_compressed"
    NEGATIVE_PROMPT_DROPPED = "negative_prompt_dropped"
    REFERENCES_TRUNCATED = "references_truncated"


#: Default per-penalty weights. Higher = a bigger fidelity hit. Tuned so that
#: structural concessions (losing the requested mode, dropping determinism,
#: splitting a take) dominate cosmetic ones (an fps nudge), letting the router
#: prefer the backend that needs the *least* invasive plan.
DEFAULT_PENALTY_WEIGHTS: dict[FidelityPenalty, float] = {
    FidelityPenalty.KEYFRAME_SYNTH: 3.0,
    FidelityPenalty.MODE_DOWNGRADE: 4.0,
    FidelityPenalty.DURATION_SPLIT: 2.0,  # per *extra* segment beyond the first
    FidelityPenalty.DURATION_CLAMP: 2.5,
    FidelityPenalty.FPS_CLAMP: 0.5,
    FidelityPenalty.RESOLUTION_CLAMP: 1.0,
    FidelityPenalty.ASPECT_CLAMP: 1.5,
    FidelityPenalty.SEED_DROPPED: 2.0,
    FidelityPenalty.PROMPT_COMPRESSED: 1.0,
    FidelityPenalty.NEGATIVE_PROMPT_DROPPED: 0.5,
    FidelityPenalty.REFERENCES_TRUNCATED: 1.5,
}


class FidelityNote(BaseModel):
    """One scored concession: a penalty category, its weight, and a human reason."""

    model_config = ConfigDict(frozen=True)

    penalty: FidelityPenalty
    weight: float
    reason: str


class RenderPlan(BaseModel):
    """An executable plan that always yields a valid request for a backend.

    Attributes:
        backend: The target backend's name (from its :class:`CapabilityProfile`).
        request: The original canonical request (unmodified, for provenance).
        steps: Ordered :class:`PlanStep` s — execute front to back.
        notes: The scored concessions that produced ``fidelity_cost``.
        feasible: ``True`` when the plan faithfully covers the request. A plan can
            still be feasible *with* concessions; ``False`` is reserved for a
            backend that cannot render the request at all (e.g. an i2v-only backend
            asked for i2v but given no driving frame and unable to synthesize one).
        rationale: A human-readable explanation of every translation/downgrade.
    """

    model_config = ConfigDict(frozen=True)

    backend: str
    request: CanonicalVideoRequest
    steps: tuple[PlanStep, ...]
    notes: tuple[FidelityNote, ...] = ()
    feasible: bool = True
    rationale: str = ""

    @property
    def fidelity_cost(self) -> float:
        """The summed fidelity penalty — lower is a more faithful plan.

        An infeasible plan is :data:`float('inf')` so a router comparing backends
        never picks one that cannot render the request.
        """
        if not self.feasible:
            return float("inf")
        return round(sum(n.weight for n in self.notes), 4)

    @property
    def render_steps(self) -> tuple[PlanStep, ...]:
        """Just the RENDER steps (the segments that consume video-seconds)."""
        return tuple(s for s in self.steps if s.kind is StepKind.RENDER)

    @property
    def segment_count(self) -> int:
        """How many video render segments the plan chains."""
        return len(self.render_steps)

    @property
    def total_render_seconds(self) -> float:
        """Total video-seconds the plan's render steps consume (budget estimate)."""
        return round(sum(s.duration_s for s in self.render_steps), 4)


__all__ = [
    "DEFAULT_PENALTY_WEIGHTS",
    "CanonicalVideoRequest",
    "FidelityNote",
    "FidelityPenalty",
    "PlanStep",
    "PostOp",
    "RenderPlan",
    "StepKind",
]
