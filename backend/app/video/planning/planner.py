"""The capability-aware request planner + degradation negotiator (pure logic).

Given a desired :class:`~app.video.planning.plan.CanonicalVideoRequest` and a
target :class:`~app.video.planning.capabilities.CapabilityProfile`, :func:`plan`
produces a :class:`~app.video.planning.plan.RenderPlan` that **always yields a
valid request for that backend** by automatically translating and downgrading:

* backend lacks reference-to-video → synthesize a keyframe, then image-to-video;
* backend lacks first-last-frame → text/image-to-video then a continuation toward
  the end frame (the end frame becomes a continuation post-marker);
* requested duration exceeds the per-call max → split into N chained continuation
  segments with overlap / hard-cut stitch markers and last-frame hand-off;
* unsupported fps / resolution / aspect → clamp to the nearest supported value and
  attach the up/down-scale / retime / reframe post-op the pipeline must apply;
* missing seed support → drop the seed and flag the loss of determinism;
* prompt over the char budget → compress it (deterministic, structure-preserving).

The planner emits, alongside the ordered steps, a human-readable ``rationale`` and
a ``fidelity_cost`` so a router can compare candidate backends and pick the one
that needs the least invasive plan. No network, no env, no RNG — fully testable.
"""

from __future__ import annotations

from .capabilities import AspectRatio, CapabilityProfile, VideoMode, resolution_height
from .plan import (
    DEFAULT_PENALTY_WEIGHTS,
    CanonicalVideoRequest,
    FidelityNote,
    FidelityPenalty,
    PlanStep,
    PostOp,
    RenderPlan,
    StepKind,
)

# --------------------------------------------------------------------------- #
# Prompt compression (deterministic)
# --------------------------------------------------------------------------- #


def compress_prompt(prompt: str, max_chars: int) -> str:
    """Shrink ``prompt`` to ``<= max_chars`` deterministically, keeping structure.

    Strategy (each only applied if still over budget): collapse runs of
    whitespace; drop a trailing cinematic-finish clause if present; then trim
    whole comma/period-separated clauses from the *end* (the head — subject and
    action — is the most load-bearing); finally hard-truncate on a word boundary
    with an ellipsis. Idempotent and free of randomness.
    """
    text = " ".join(prompt.split())
    if len(text) <= max_chars:
        return text

    # Trim trailing clauses (split on ", " / ". ") until under budget; the leading
    # clause(s) carry subject+action and are kept.
    seps = (". ", ", ")
    while len(text) > max_chars:
        cut = max((text.rfind(s) for s in seps), default=-1)
        if cut <= 0:
            break
        text = text[:cut].rstrip(" .,")

    if len(text) <= max_chars:
        return text

    # Hard fallback: word-boundary truncate, leaving room for an ellipsis.
    budget = max(1, max_chars - 1)
    clipped = text[:budget]
    space = clipped.rfind(" ")
    if space > 0:
        clipped = clipped[:space]
    return f"{clipped.rstrip(' .,')}…"[:max_chars]


# --------------------------------------------------------------------------- #
# Internal negotiation state
# --------------------------------------------------------------------------- #


class _Builder:
    """Accumulates steps, notes, and rationale lines while negotiating one request."""

    def __init__(
        self,
        request: CanonicalVideoRequest,
        profile: CapabilityProfile,
        weights: dict[FidelityPenalty, float],
    ) -> None:
        self.request = request
        self.profile = profile
        self.weights = weights
        self.notes: list[FidelityNote] = []
        self.rationale: list[str] = []
        self.feasible = True

    def penalize(self, penalty: FidelityPenalty, reason: str, *, scale: float = 1.0) -> None:
        weight = self.weights.get(penalty, 0.0) * scale
        self.notes.append(FidelityNote(penalty=penalty, weight=weight, reason=reason))
        self.rationale.append(reason)


def _clamp_format(
    request: CanonicalVideoRequest, profile: CapabilityProfile, builder: _Builder
) -> tuple[int, str, AspectRatio, list[PostOp]]:
    """Resolve fps/resolution/aspect against the profile, recording concessions.

    Returns the clamped ``(fps, resolution, aspect)`` plus the format post-ops the
    pipeline must apply to converge on the canonical values.
    """
    post: list[PostOp] = []

    fps = profile.clamp_fps(request.fps)
    if fps != request.fps:
        builder.penalize(
            FidelityPenalty.FPS_CLAMP,
            f"fps {request.fps} unsupported → clamped to {fps}; pipeline retimes.",
        )
        post.append(PostOp.RETIME_FPS)

    resolution = profile.clamp_resolution(request.resolution)
    if resolution_height(resolution) != resolution_height(request.resolution):
        want_h = resolution_height(request.resolution)
        got_h = resolution_height(resolution)
        builder.penalize(
            FidelityPenalty.RESOLUTION_CLAMP,
            f"resolution {request.resolution} unsupported → {resolution}; "
            f"pipeline {'upscales' if got_h < want_h else 'downscales'}.",
        )
        post.append(PostOp.UPSCALE if got_h < want_h else PostOp.DOWNSCALE)

    aspect = profile.clamp_aspect(request.aspect)
    if aspect != request.aspect:
        builder.penalize(
            FidelityPenalty.ASPECT_CLAMP,
            f"aspect {request.aspect} unsupported → {aspect}; pipeline reframes "
            f"(pad/crop) to {request.aspect}.",
        )
        post.append(PostOp.REFRAME_ASPECT)

    return fps, resolution, aspect, post


def _resolve_seed(
    request: CanonicalVideoRequest, profile: CapabilityProfile, builder: _Builder
) -> int | None:
    if request.seed is None:
        return None
    if profile.supports_seed:
        return request.seed
    builder.penalize(
        FidelityPenalty.SEED_DROPPED,
        f"seed {request.seed} requested but backend has no seed control → dropped; "
        "render is non-deterministic.",
    )
    return None


def _resolve_prompt(
    request: CanonicalVideoRequest, profile: CapabilityProfile, builder: _Builder
) -> tuple[str, str | None]:
    prompt = request.prompt
    if len(prompt) > profile.max_prompt_chars:
        compressed = compress_prompt(prompt, profile.max_prompt_chars)
        builder.penalize(
            FidelityPenalty.PROMPT_COMPRESSED,
            f"prompt {len(prompt)} chars > backend cap {profile.max_prompt_chars} "
            f"→ compressed to {len(compressed)}.",
        )
        prompt = compressed

    negative = request.negative_prompt
    if negative and not profile.supports_negative_prompt:
        builder.penalize(
            FidelityPenalty.NEGATIVE_PROMPT_DROPPED,
            "negative prompt unsupported by backend → dropped.",
        )
        negative = None
    return prompt, negative


def _resolve_references(
    request: CanonicalVideoRequest, profile: CapabilityProfile, builder: _Builder
) -> tuple[str, ...]:
    refs = request.reference_image_urls
    cap = profile.max_reference_images
    if cap >= 0 and len(refs) > cap:
        builder.penalize(
            FidelityPenalty.REFERENCES_TRUNCATED,
            f"{len(refs)} reference images > backend cap {cap} → kept first {cap}.",
            scale=(len(refs) - cap),
        )
        refs = refs[:cap]
    return refs


# --------------------------------------------------------------------------- #
# Mode resolution — pick the executable native mode + any translation pre-step
# --------------------------------------------------------------------------- #


def _resolve_mode(
    request: CanonicalVideoRequest, profile: CapabilityProfile, builder: _Builder
) -> tuple[VideoMode | None, bool]:
    """Pick the native mode to execute; flag whether a keyframe synth is needed.

    Returns ``(executable_mode, needs_keyframe_synth)``. ``executable_mode`` is
    ``None`` when the request is infeasible on this backend (builder.feasible set).
    """
    want = request.mode

    # Native support — no translation.
    if profile.supports_mode(want):
        return want, False

    # reference_to_video unsupported → synthesize a locked keyframe, then i2v.
    if want is VideoMode.REFERENCE_TO_VIDEO:
        if profile.supports_mode(VideoMode.IMAGE_TO_VIDEO):
            builder.penalize(
                FidelityPenalty.KEYFRAME_SYNTH,
                "reference-to-video unsupported → synthesize an identity keyframe "
                "from the references, then image-to-video.",
            )
            return VideoMode.IMAGE_TO_VIDEO, True
        if profile.supports_mode(VideoMode.TEXT_TO_VIDEO):
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                "reference-to-video and image-to-video both unsupported → "
                "text-to-video (identity carried via the prompt only).",
            )
            return VideoMode.TEXT_TO_VIDEO, False
        builder.feasible = False
        return None, False

    # first_last_frame unsupported → image/text → continuation toward the end frame.
    if want is VideoMode.FIRST_LAST_FRAME:
        if profile.supports_mode(VideoMode.IMAGE_TO_VIDEO) and request.first_frame_url:
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                "first-last-frame unsupported → image-to-video from the first frame; "
                "the last frame becomes a continuation target downstream.",
            )
            return VideoMode.IMAGE_TO_VIDEO, False
        if profile.supports_mode(VideoMode.TEXT_TO_VIDEO):
            needs_kf = bool(request.first_frame_url) and profile.can_synthesize_keyframe
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                "first-last-frame unsupported → text-to-video toward the described "
                "composition; endpoints approximated.",
            )
            return VideoMode.TEXT_TO_VIDEO, needs_kf and False
        builder.feasible = False
        return None, False

    # image_to_video unsupported → if we have references or can synth, t2v; else infeasible.
    if want is VideoMode.IMAGE_TO_VIDEO:
        if profile.supports_mode(VideoMode.TEXT_TO_VIDEO):
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                "image-to-video unsupported → text-to-video (driving frame described "
                "in the prompt only).",
            )
            return VideoMode.TEXT_TO_VIDEO, False
        builder.feasible = False
        return None, False

    # video_continuation / instruction_edit unsupported → fall back to i2v from the
    # source's last frame if possible, else t2v.
    if want in (VideoMode.VIDEO_CONTINUATION, VideoMode.INSTRUCTION_EDIT):
        if profile.supports_mode(VideoMode.IMAGE_TO_VIDEO):
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                f"{want.value} unsupported → image-to-video seeded by the source "
                "clip's last frame.",
            )
            return VideoMode.IMAGE_TO_VIDEO, False
        if profile.supports_mode(VideoMode.TEXT_TO_VIDEO):
            builder.penalize(
                FidelityPenalty.MODE_DOWNGRADE,
                f"{want.value} unsupported → text-to-video; prior-clip context lost.",
            )
            return VideoMode.TEXT_TO_VIDEO, False
        builder.feasible = False
        return None, False

    # text_to_video unsupported with nothing to fall back to.
    builder.feasible = False
    return None, False


# --------------------------------------------------------------------------- #
# Duration splitting into chained continuation segments
# --------------------------------------------------------------------------- #


def _segment_durations(total_s: float, profile: CapabilityProfile) -> list[float]:
    """Split ``total_s`` into a list of per-call durations within the window.

    Greedily takes the largest clamped duration the backend allows until the
    remainder is covered; the **final segment carries only the remainder**, itself
    clamped to a valid backend duration so the chain never renders far more than
    requested (this conserves scarce video-seconds, §11). When overlap is
    supported, each non-first segment re-renders the ``overlap_s`` tail of its
    predecessor for a clean stitch, so a segment contributes ``len - overlap`` of
    *new* timeline; the math accounts for that.
    """
    max_seg = profile.clamp_duration(profile.max_duration_s)
    if total_s <= profile.clamp_duration(total_s) + 1e-9:
        return [profile.clamp_duration(total_s)]

    overlap = profile.overlap_s if profile.supports_continuation_overlap else 0.0
    durations: list[float] = []
    covered = 0.0  # new timeline seconds covered so far
    while covered < total_s - 1e-9:
        remaining = total_s - covered
        # New timeline a *fresh* full-length segment would add (first seg adds its
        # whole length; later segments lose the re-rendered overlap window).
        new_if_full = max_seg if not durations else max_seg - overlap
        if remaining <= new_if_full + 1e-9:
            # Final segment: render only what's left (+ the overlap it re-renders).
            want = remaining + (overlap if durations else 0.0)
            durations.append(profile.clamp_duration(want))
            break
        durations.append(max_seg)
        covered += new_if_full
        if len(durations) > 64:  # safety against a pathological tiny window
            break
    return durations


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #


def plan(
    request: CanonicalVideoRequest,
    profile: CapabilityProfile,
    *,
    weights: dict[FidelityPenalty, float] | None = None,
) -> RenderPlan:
    """Produce an executable :class:`RenderPlan` for ``request`` on ``profile``.

    The returned plan is always *runnable* on the backend (every step uses an
    accepted mode + clamped format), even when that requires translating the mode,
    synthesizing a keyframe, splitting the duration, clamping the format, dropping
    the seed, or compressing the prompt. Concessions are scored into
    :attr:`RenderPlan.fidelity_cost` and explained in :attr:`RenderPlan.rationale`.

    A plan is marked ``feasible=False`` (and costs ``inf``) only when the backend
    cannot render the request *at all* — e.g. it supports no mode reachable from
    the requested one. The plan is still returned (with whatever steps could be
    formed) so the caller can introspect why.
    """
    w = weights or DEFAULT_PENALTY_WEIGHTS
    builder = _Builder(request, profile, w)

    exec_mode, needs_keyframe = _resolve_mode(request, profile, builder)
    fps, resolution, aspect, fmt_post = _clamp_format(request, profile, builder)
    seed = _resolve_seed(request, profile, builder)
    prompt, negative = _resolve_prompt(request, profile, builder)
    refs = _resolve_references(request, profile, builder)

    steps: list[PlanStep] = []
    idx = 0

    if not builder.feasible or exec_mode is None:
        rationale = _format_rationale(builder, profile, infeasible=True)
        return RenderPlan(
            backend=profile.name,
            request=request,
            steps=tuple(steps),
            notes=tuple(builder.notes),
            feasible=False,
            rationale=rationale,
        )

    # 1) Optional keyframe-synthesis pre-step (r2v → i2v translation).
    synth_image_marker: str | None = None
    if needs_keyframe:
        synth_image_marker = f"synth:{request.shot_id or 'kf'}"
        steps.append(
            PlanStep(
                index=idx,
                kind=StepKind.SYNTHESIZE_KEYFRAME,
                prompt=prompt,
                reference_image_urls=refs,
                note=(
                    "Synthesize an identity-locked keyframe from references; its "
                    "output feeds the following image-to-video render."
                ),
            )
        )
        idx += 1

    # 2) The driving frame for the (possibly translated) render.
    image_url = request.image_url
    if exec_mode is VideoMode.IMAGE_TO_VIDEO:
        if synth_image_marker is not None:
            image_url = synth_image_marker
        elif image_url is None and request.first_frame_url:
            image_url = request.first_frame_url  # flf → i2v from first frame
        elif image_url is None and refs:
            image_url = refs[0]

    # 3) Duration: split into chained segments if it exceeds the per-call window.
    durations = _segment_durations(request.duration_s, profile)
    seg_count = len(durations)
    if seg_count > 1:
        builder.penalize(
            FidelityPenalty.DURATION_SPLIT,
            f"requested {request.duration_s:g}s exceeds per-call max "
            f"{profile.max_duration_s:g}s → chained into {seg_count} segments"
            + (
                f" with {profile.overlap_s:g}s overlap stitch."
                if profile.supports_continuation_overlap
                else " with hard-cut stitch."
            ),
            scale=(seg_count - 1),
        )
    else:
        clamped = durations[0]
        if abs(clamped - request.duration_s) > 1e-6 and clamped < request.duration_s:
            builder.penalize(
                FidelityPenalty.DURATION_CLAMP,
                f"requested {request.duration_s:g}s snapped to backend duration {clamped:g}s.",
            )

    stitch_op = (
        PostOp.STITCH_OVERLAP if profile.supports_continuation_overlap else PostOp.STITCH_CUT
    )

    for seg_i, seg_dur in enumerate(durations):
        is_first = seg_i == 0
        is_last = seg_i == seg_count - 1
        post: list[PostOp] = list(fmt_post)

        if seg_count > 1 and not is_last:
            post.append(PostOp.EXTRACT_LAST_FRAME)
            post.append(stitch_op)

        # Per-segment mode: the first uses the resolved mode; later segments are
        # continuations seeded by the prior segment's extracted last frame.
        if is_first:
            seg_mode = exec_mode
            seg_image = image_url
            seg_first = request.first_frame_url if exec_mode is VideoMode.FIRST_LAST_FRAME else None
            seg_last = request.last_frame_url if exec_mode is VideoMode.FIRST_LAST_FRAME else None
            seg_refs = refs if exec_mode is VideoMode.REFERENCE_TO_VIDEO else ()
            seg_source = (
                request.source_video_url
                if exec_mode in (VideoMode.VIDEO_CONTINUATION, VideoMode.INSTRUCTION_EDIT)
                else None
            )
        else:
            # Continuation segment: prefer native continuation; else i2v from the
            # prior last frame; else (t2v-only backend) a hard-cut t2v segment.
            prev = f"lastframe:{seg_i - 1}"
            if profile.supports_mode(VideoMode.VIDEO_CONTINUATION):
                seg_mode = VideoMode.VIDEO_CONTINUATION
                seg_image = None
                seg_source = prev
            elif profile.supports_mode(VideoMode.IMAGE_TO_VIDEO):
                seg_mode = VideoMode.IMAGE_TO_VIDEO
                seg_image = prev
                seg_source = None
            else:
                seg_mode = VideoMode.TEXT_TO_VIDEO
                seg_image = None
                seg_source = None
            seg_first = seg_last = None
            seg_refs = ()

        steps.append(
            PlanStep(
                index=idx,
                kind=StepKind.RENDER,
                mode=seg_mode,
                prompt=prompt,
                negative_prompt=negative,
                duration_s=seg_dur,
                fps=fps,
                resolution=resolution,
                aspect=aspect,
                seed=seed,
                reference_image_urls=seg_refs,
                image_url=seg_image,
                first_frame_url=seg_first,
                last_frame_url=seg_last,
                source_video_url=seg_source,
                segment_index=seg_i,
                segment_count=seg_count,
                post_ops=tuple(post),
                note=_segment_note(seg_i, seg_count, seg_mode, is_last),
            )
        )
        idx += 1

    rationale = _format_rationale(builder, profile, infeasible=False)
    return RenderPlan(
        backend=profile.name,
        request=request,
        steps=tuple(steps),
        notes=tuple(builder.notes),
        feasible=True,
        rationale=rationale,
    )


def _segment_note(seg_i: int, seg_count: int, mode: VideoMode, is_last: bool) -> str:
    if seg_count == 1:
        return f"Single {mode.value} render."
    if seg_i == 0:
        return f"Opening segment ({mode.value}); its last frame seeds segment 2."
    tail = "final segment" if is_last else f"segment {seg_i + 1}/{seg_count}"
    return f"Continuation {tail} ({mode.value}) seeded by the prior last frame."


def _format_rationale(builder: _Builder, profile: CapabilityProfile, *, infeasible: bool) -> str:
    header = (
        f"[{profile.name}] target mode {builder.request.mode.value}, "
        f"{builder.request.duration_s:g}s @ {builder.request.fps}fps "
        f"{builder.request.resolution} {builder.request.aspect}."
    )
    if infeasible:
        body = "INFEASIBLE: backend exposes no render mode reachable from the request."
        lines = [header, body, *builder.rationale]
        return "\n".join(lines)
    if not builder.rationale:
        return f"{header}\nNative fit — no translation or degradation required."
    return "\n".join([header, "Concessions:", *(f"- {r}" for r in builder.rationale)])


__all__ = ["compress_prompt", "plan"]
