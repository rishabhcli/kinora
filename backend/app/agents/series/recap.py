""""Previously on" recap synthesis (§7, §8.2, §8.7, §11).

When a reader opens Volume 3, the Showrunner assembles a "previously on" recap of
the most important prior beats. Two constraints make this a *selection* problem,
not a dump:

* **budget** — video is the scarce, hard-capped unit (§11). A recap must fit in a
  small video-second budget, so the Showrunner picks the highest-value beats and
  stops when the budget is spent. Because a recap reuses *already accepted clips*
  from episodic memory (§8.2), it costs near-zero *new* video-seconds (§8.7) — the
  budget here bounds recap *length*, not fresh generation;
* **relevance** — a beat's recap value blends recency (later beats matter more),
  arc intensity (the dramatic high points), and motif relevance (a beat that
  planted a motif now paying off).

:func:`select_recap_beats` is the pure selection policy; the Showrunner fills the
narration prose over the resulting :class:`~app.agents.contracts.RecapSpec`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.contracts import (
    ArcBeat,
    Motif,
    RecapItem,
    RecapSpec,
)

#: Default per-beat recap clip length (s) — short, montage-style.
_DEFAULT_BEAT_SECONDS = 3.0
#: Weighting of the three relevance signals (sum to 1.0).
_W_RECENCY = 0.4
_W_INTENSITY = 0.4
_W_MOTIF = 0.2


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _recency_weight(beat: ArcBeat, *, latest_volume: int, latest_beat: int) -> float:
    """Newer beats weigh more; decays smoothly with distance into the past."""
    if latest_volume <= 0 and latest_beat <= 0:
        return 1.0
    vol_gap = max(0, latest_volume - beat.volume_index)
    # A whole volume back halves the weight; beat distance is a gentler decay.
    span = max(1, latest_beat) if beat.volume_index == latest_volume else max(1, latest_beat)
    beat_gap = max(0, latest_beat - beat.beat_index) if beat.volume_index == latest_volume else span
    vol_decay = 0.5**vol_gap
    beat_decay = 1.0 - 0.5 * min(1.0, beat_gap / span)
    return _clamp01(vol_decay * beat_decay)


def recap_weight(
    beat: ArcBeat,
    *,
    latest_volume: int,
    latest_beat: int,
    motif_beats: frozenset[tuple[int, int]] = frozenset(),
) -> float:
    """The recap value of a prior beat (recency × intensity × motif relevance)."""
    recency = _recency_weight(beat, latest_volume=latest_volume, latest_beat=latest_beat)
    intensity = _clamp01(beat.intensity)
    motif = 1.0 if (beat.volume_index, beat.beat_index) in motif_beats else 0.0
    return _W_RECENCY * recency + _W_INTENSITY * intensity + _W_MOTIF * motif


def select_recap_beats(
    prior_beats: Iterable[ArcBeat],
    *,
    for_volume: int,
    budget_s: float,
    beat_seconds: float = _DEFAULT_BEAT_SECONDS,
    motifs: Sequence[Motif] = (),
) -> RecapSpec:
    """Pick the highest-value prior beats that fit a recap budget (§7, §11).

    Only beats from *before* ``for_volume`` are eligible (a recap looks back). The
    beats are scored by :func:`recap_weight`, taken in descending value until the
    ``budget_s`` is spent, then re-sorted into reading order for playback. The
    returned :class:`RecapSpec` has empty ``narration`` — the Showrunner fills it.
    """
    eligible = [b for b in prior_beats if b.volume_index < for_volume]
    if not eligible:
        return RecapSpec(for_volume=for_volume, items=[], total_target_s=0.0)

    latest_volume = max(b.volume_index for b in eligible)
    same_vol = [b for b in eligible if b.volume_index == latest_volume]
    latest_beat = max((b.beat_index for b in same_vol), default=0)

    motif_beats = frozenset(
        (m.planted_volume, m.planted_beat) for m in motifs
    )

    scored = sorted(
        eligible,
        key=lambda b: (
            -recap_weight(
                b,
                latest_volume=latest_volume,
                latest_beat=latest_beat,
                motif_beats=motif_beats,
            ),
            b.volume_index,
            b.beat_index,
        ),
    )

    chosen: list[ArcBeat] = []
    spent = 0.0
    for beat in scored:
        if spent + beat_seconds > budget_s + 1e-9:
            continue
        chosen.append(beat)
        spent += beat_seconds

    chosen.sort(key=lambda b: (b.volume_index, b.beat_index))
    items = [
        RecapItem(
            volume_index=b.volume_index,
            beat_index=b.beat_index,
            summary=b.summary,
            weight=round(
                recap_weight(
                    b,
                    latest_volume=latest_volume,
                    latest_beat=latest_beat,
                    motif_beats=motif_beats,
                ),
                4,
            ),
            est_seconds=beat_seconds,
            motif_ids=[
                m.motif_id
                for m in motifs
                if (m.planted_volume, m.planted_beat) == (b.volume_index, b.beat_index)
            ],
        )
        for b in chosen
    ]
    return RecapSpec(for_volume=for_volume, items=items, total_target_s=round(spent, 4))


def recap_prompt_payload(spec: RecapSpec) -> dict[str, object]:
    """The structured payload handed to the Showrunner to write the recap prose."""
    return {
        "task": "synthesize_recap",
        "for_volume": spec.for_volume,
        "total_target_s": spec.total_target_s,
        "beats": [
            {
                "volume_index": item.volume_index,
                "beat_index": item.beat_index,
                "summary": item.summary,
                "weight": item.weight,
            }
            for item in spec.items
        ],
    }


__all__ = [
    "recap_prompt_payload",
    "recap_weight",
    "select_recap_beats",
]
