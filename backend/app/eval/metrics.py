"""Metric definitions for the Track-3 eval harness (kinora.md §13).

This is the pure, typed, deterministic core of the "measurable efficiency gain
over single-agent baselines" proof. Every metric in §13 is defined here twice:

* an **embedding-space** function that operates on already-computed vectors /
  numbers (``ccs_from_embeddings``, ``style_drift`` …) — these are pure and are
  exactly what the unit tests pin against one-hot embeddings for *known* values;
* a thin **async wrapper** (``character_consistency_score``) that embeds raw
  bytes through an injected :class:`~app.memory.interfaces.Embedder` and then
  defers to the pure function — so production uses the real multimodal embedder
  and tests inject a deterministic one.

The metrics, verbatim from §13:

* **CCS** — mean cosine of a character's per-shot crop embeddings vs the locked
  reference embedding (higher = more consistent identity).
* **Accepted-footage efficiency** — ``(1 − rejected/total) × 100`` (QA-passed
  seconds per 100s of generation budget; the headline budget number).
* **Regeneration rate** — ``regens / total_shots`` (lower = memory conditioned
  each shot correctly the first time).
* **Style drift** — variance of a scene's style embeddings about their centroid
  (lower = a more coherent look).
* **Latency-to-first-frame** — seek → first coherent frame (the keyframe bridge)
  and seek → first full video (one render).
* **Buffer health** — the fraction of reading-time the committed buffer stayed
  at/above the low watermark ``L`` plus the count of visible stalls (ahead == 0).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.memory.interfaces import Embedder
from app.providers.embeddings import cosine

#: A dense embedding vector (the shared image+text space, §8 / providers).
Vector = list[float]


# --------------------------------------------------------------------------- #
# Character Consistency Score — CCS (§13, §9.5)
# --------------------------------------------------------------------------- #


def ccs_from_embeddings(
    crop_embeddings: Sequence[Vector], locked_ref_embedding: Vector
) -> float:
    """Mean ``cosine(crop, locked_ref)`` across a character's crops (§13).

    The §13 Character Consistency Score for **one** character: the mean
    appearance cosine of every shot's crop embedding against that character's
    locked-reference embedding. Higher is better. This is the pure,
    injected-embeddings variant the unit tests pin to known cosines.

    Returns ``0.0`` for an empty crop list — a character measured in no shots
    carries no consistency evidence (callers should only score characters that
    actually appear).
    """
    crops = list(crop_embeddings)
    if not crops:
        return 0.0
    ref = list(locked_ref_embedding)
    return math.fsum(cosine(list(crop), ref) for crop in crops) / len(crops)


async def character_consistency_score(
    crops: Sequence[bytes], locked_ref: bytes, *, embedder: Embedder
) -> float:
    """CCS for a character from raw crop bytes vs a locked reference (§13).

    Embeds the crops + the locked reference through the injected ``embedder``
    (the shared image+text space, so cosine is meaningful) and defers to
    :func:`ccs_from_embeddings`. Tests use the pure variant with injected
    embeddings; production passes the real multimodal embedder.
    """
    crop_list = list(crops)
    if not crop_list:
        return 0.0
    crop_vecs = await embedder.embed_images(crop_list)
    ref_vec = (await embedder.embed_images([locked_ref]))[0]
    return ccs_from_embeddings(crop_vecs, ref_vec)


# --------------------------------------------------------------------------- #
# Accepted-footage efficiency (§13, §11.1) — the headline budget number
# --------------------------------------------------------------------------- #


def accepted_footage_efficiency(total_seconds: float, rejected_seconds: float) -> float:
    """``(1 − rejected/total) × 100`` — QA-passed video per 100s of budget (§13).

    The fraction of generation budget that produced *accepted* footage, scaled
    to 0–100. With nothing generated (``total_seconds <= 0``) there is no waste,
    so efficiency is defined as ``100.0``. The result is clamped to ``[0, 100]``
    so a (nonsensical) ``rejected > total`` can never read below zero.
    """
    if total_seconds <= 0.0:
        return 100.0
    fraction = 1.0 - (rejected_seconds / total_seconds)
    return max(0.0, min(1.0, fraction)) * 100.0


# --------------------------------------------------------------------------- #
# Regeneration rate (§13)
# --------------------------------------------------------------------------- #


def regeneration_rate(regens: int, total_shots: int) -> float:
    """``regens / total_shots`` — lower is better (§13).

    The crew should beat the single-agent baseline here because memory conditions
    each shot correctly the first time, so fewer shots fail QA and need a regen.
    Returns ``0.0`` when no shots were attempted.
    """
    if total_shots <= 0:
        return 0.0
    return regens / total_shots


# --------------------------------------------------------------------------- #
# Style drift (§13)
# --------------------------------------------------------------------------- #


def style_drift(style_embeddings: Sequence[Vector]) -> float:
    """Variance of a scene's style embeddings about their centroid (§13).

    Total variance ``mean_i ||e_i − mean(e)||²`` — the trace of the covariance.
    Lower means a more coherent look across the scene; identical style vectors
    give ``0.0``. Fewer than two embeddings have no spread, so return ``0.0``.

    Raises:
        ValueError: if the embeddings are not all the same dimension.
    """
    vectors = [list(e) for e in style_embeddings]
    n = len(vectors)
    if n < 2:
        return 0.0
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        raise ValueError("style embeddings must share one dimension")
    centroid = [math.fsum(v[i] for v in vectors) / n for i in range(dim)]
    total = math.fsum(
        math.fsum((v[i] - centroid[i]) ** 2 for i in range(dim)) for v in vectors
    )
    return total / n


# --------------------------------------------------------------------------- #
# Latency-to-first-frame on seek (§13, §4.8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LatencyToFirstFrame:
    """The two §13 seek latencies, in seconds (clamped to ``>= 0``)."""

    #: Seek → something coherent on screen (≈ one frame: the keyframe bridge, §4.8).
    coherent_s: float
    #: Seek → full video playing (≈ one render).
    full_video_s: float


def latency_to_first_frame(
    seek_ts: float, first_coherent_ts: float, first_full_video_ts: float
) -> LatencyToFirstFrame:
    """Compute the §13 seek→coherent and seek→full-video latencies.

    ``*_ts`` are wall-clock timestamps in seconds; the latencies are the deltas
    from the seek, clamped to ``>= 0`` (nothing can render before the seek).
    """
    return LatencyToFirstFrame(
        coherent_s=max(0.0, first_coherent_ts - seek_ts),
        full_video_s=max(0.0, first_full_video_ts - seek_ts),
    )


# --------------------------------------------------------------------------- #
# Buffer health (§13, §4.5/§4.10) — the sawtooth quality
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BufferSample:
    """One point on the committed-buffer occupancy sawtooth (§4.10).

    This is the per-tick record the buffer-trace produces and the §13
    buffer-health metric consumes. It also serializes 1:1 to the shared API
    contract item ``{t, committed_seconds_ahead, low, high}``.
    """

    #: Wall-clock seconds since the trace started.
    t: float
    #: Committed video-seconds buffered ahead of the focus playhead.
    committed_seconds_ahead: float
    #: The low watermark ``L`` active for this sample.
    low: float
    #: The high watermark ``H`` active for this sample.
    high: float

    def to_contract(self) -> dict[str, float]:
        """Project to the exact frontend contract item (§ shared API)."""
        return {
            "t": round(self.t, 6),
            "committed_seconds_ahead": round(self.committed_seconds_ahead, 6),
            "low": round(self.low, 6),
            "high": round(self.high, 6),
        }


@dataclass(frozen=True, slots=True)
class BufferHealth:
    """The §13 buffer-health verdict over a sawtooth trace."""

    #: Fraction of reading-time the buffer stayed at/above ``L`` (target > 0.99).
    fraction_above_low: float
    #: Count of visible stalls — onsets of ``committed_seconds_ahead <= 0`` (target 0).
    stalls: int
    #: Number of samples in the trace.
    samples: int
    #: Total reading-time spanned by the trace, in seconds.
    duration_s: float


def buffer_health(
    trace: Sequence[BufferSample], *, low_watermark: float | None = None
) -> BufferHealth:
    """Fraction of time the buffer stayed ``>= L`` + the stall count (§13).

    The fraction is **time-weighted**: each sample holds until the next one (a
    step function), so uneven tick spacing is handled correctly. A *stall* is the
    onset of an empty buffer (``committed_seconds_ahead <= 0`` after being
    positive), i.e. a visible playback stall. ``low_watermark`` overrides each
    sample's own ``low`` when given (e.g. to score against a fixed ``L``).
    """
    samples = list(trace)
    n = len(samples)
    if n == 0:
        return BufferHealth(fraction_above_low=1.0, stalls=0, samples=0, duration_s=0.0)

    def low_for(sample: BufferSample) -> float:
        return sample.low if low_watermark is None else low_watermark

    stalls = 0
    in_stall = False
    above_time = 0.0
    total_time = 0.0
    for i, sample in enumerate(samples):
        stalled_now = sample.committed_seconds_ahead <= 0.0
        if stalled_now and not in_stall:
            stalls += 1
        in_stall = stalled_now
        dt = max(0.0, samples[i + 1].t - sample.t) if i < n - 1 else 0.0
        total_time += dt
        if sample.committed_seconds_ahead >= low_for(sample):
            above_time += dt

    if total_time <= 0.0:
        # Degenerate (single sample / zero-length): fall back to a count fraction.
        above_count = sum(
            1 for s in samples if s.committed_seconds_ahead >= low_for(s)
        )
        span = max(0.0, samples[-1].t - samples[0].t) if n > 1 else 0.0
        return BufferHealth(
            fraction_above_low=above_count / n,
            stalls=stalls,
            samples=n,
            duration_s=span,
        )

    return BufferHealth(
        fraction_above_low=above_time / total_time,
        stalls=stalls,
        samples=n,
        duration_s=total_time,
    )


__all__ = [
    "BufferHealth",
    "BufferSample",
    "LatencyToFirstFrame",
    "Vector",
    "accepted_footage_efficiency",
    "ccs_from_embeddings",
    "character_consistency_score",
    "latency_to_first_frame",
    "regeneration_rate",
    "style_drift",
]
