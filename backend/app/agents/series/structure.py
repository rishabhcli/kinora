"""Automatic episode / act boundary detection from the pacing curve (§7).

Long-form adaptation needs *structure*: where the acts break, where the midpoint
turn sits, and how to slice a volume into binge-sized episodes that each end on a
cliffhanger. Rather than ask the model to guess, the Showrunner detects structure
deterministically from the pacing curve (:mod:`app.agents.series.pacing`):

* **act boundaries** fall at the largest *sustained* tension inflections — the
  classic setup→confrontation and confrontation→resolution turns, plus the
  mid-act midpoint;
* **episode boundaries** cut the volume so each episode ends on a *local tension
  peak* (a cliffhanger), with the final episode closing on the climax/resolution.

All pure functions of a :class:`~app.agents.contracts.PacingCurve`.
"""

from __future__ import annotations

from app.agents.contracts import ActBoundary, EpisodeBoundary, PacingCurve
from app.agents.series.pacing import smooth_curve


def _local_maxima(values: list[float]) -> list[int]:
    """Indices that are >= both neighbours (plateaus pick their first index)."""
    n = len(values)
    peaks: list[int] = []
    i = 0
    while i < n:
        left_ok = i == 0 or values[i] >= values[i - 1]
        # advance over a plateau
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        right_ok = j + 1 >= n or values[i] >= values[j + 1]
        if left_ok and right_ok and (i > 0 or j + 1 < n):
            peaks.append(i)
        i = j + 1
    return peaks


def detect_act_boundaries(
    curve: PacingCurve,
    *,
    target_acts: int = 3,
    volume_index: int = 0,
) -> list[ActBoundary]:
    """Find the act breaks (and midpoint) of a volume from its pacing curve (§7).

    A 3-act split places two act breaks at the largest sustained tension
    inflections and a ``midpoint`` at the mid-act turn. The boundaries are returned
    in reading order. With too-few samples to support ``target_acts``, returns
    whatever breaks the curve supports (possibly none).
    """
    pts = smooth_curve(curve).points
    n = len(pts)
    if n < 3 or target_acts < 2:
        return []

    values = [p.tension for p in pts]
    # Sustained inflection strength at each interior index: the magnitude of the
    # change in slope around it (a turn from build to fall, or fall to build).
    inflections: list[tuple[float, int]] = []
    for i in range(1, n - 1):
        before = values[i] - values[i - 1]
        after = values[i + 1] - values[i]
        strength = abs(after - before)
        inflections.append((strength, i))
    inflections.sort(key=lambda t: (-t[0], t[1]))

    want = target_acts - 1  # breaks between acts
    chosen = sorted({idx for _, idx in inflections[:want]})

    boundaries: list[ActBoundary] = []
    for idx in chosen:
        delta = values[idx + 1] - values[idx - 1]
        boundaries.append(
            ActBoundary(
                volume_index=pts[idx].volume_index or volume_index,
                at_beat=pts[idx].beat_index,
                kind="act",
                tension_delta=round(delta, 4),
            )
        )

    # The midpoint: the strongest inflection in the central third, labelled distinctly.
    lo, hi = n // 3, (2 * n) // 3
    central = [(s, i) for s, i in inflections if lo <= i <= hi]
    if central:
        _, mid_idx = max(central, key=lambda t: t[0])
        if all(b.at_beat != pts[mid_idx].beat_index for b in boundaries):
            boundaries.append(
                ActBoundary(
                    volume_index=pts[mid_idx].volume_index or volume_index,
                    at_beat=pts[mid_idx].beat_index,
                    kind="midpoint",
                    tension_delta=round(values[mid_idx + 1] - values[mid_idx - 1], 4),
                )
            )

    boundaries.sort(key=lambda b: b.at_beat)
    return boundaries


def detect_episode_boundaries(
    curve: PacingCurve,
    *,
    target_episodes: int = 3,
    volume_index: int = 0,
) -> list[EpisodeBoundary]:
    """Slice a volume into binge-units that end on cliffhangers (§7).

    Each episode closes on a *local tension peak* so the reader is left hanging;
    the final episode closes on the volume's climax/resolution and is flagged
    ``cliffhanger=False``. The returned episodes tile the curve with no gaps.

    With fewer beats than episodes, returns one episode per beat (degenerate but
    well-formed). With ``target_episodes <= 1`` the whole volume is one episode.
    """
    pts = smooth_curve(curve).points
    n = len(pts)
    if n == 0:
        return []
    if target_episodes <= 1:
        return [
            EpisodeBoundary(
                episode_index=0,
                volume_index=pts[0].volume_index or volume_index,
                beat_start=pts[0].beat_index,
                beat_end=pts[-1].beat_index,
                cliffhanger=False,
                peak_tension=max(p.tension for p in pts),
            )
        ]

    values = [p.tension for p in pts]
    peaks = _local_maxima(values)

    # Candidate cut points = local peaks, ranked by tension; we need
    # (target_episodes - 1) interior cuts, none at the very last sample (that's the
    # volume's own close).
    interior_peaks = [i for i in peaks if 0 < i < n - 1]
    interior_peaks.sort(key=lambda i: (-values[i], i))
    cuts = sorted(interior_peaks[: max(0, target_episodes - 1)])

    boundaries: list[EpisodeBoundary] = []
    start = 0
    for ep, cut in enumerate(cuts):
        boundaries.append(
            EpisodeBoundary(
                episode_index=ep,
                volume_index=pts[start].volume_index or volume_index,
                beat_start=pts[start].beat_index,
                beat_end=pts[cut].beat_index,
                cliffhanger=True,
                peak_tension=round(values[cut], 4),
            )
        )
        start = cut + 1

    if start < n:
        boundaries.append(
            EpisodeBoundary(
                episode_index=len(boundaries),
                volume_index=pts[start].volume_index or volume_index,
                beat_start=pts[start].beat_index,
                beat_end=pts[-1].beat_index,
                cliffhanger=False,
                peak_tension=round(max(values[start:]), 4),
            )
        )
    return boundaries


def assign_acts_to_beats(
    boundaries: list[ActBoundary], beat_indices: list[int]
) -> dict[int, int]:
    """Map each beat index to its act number (1-based), given the act breaks (§7).

    Breaks of kind ``act`` partition the beats; a ``midpoint`` does not start a new
    act. Beats before the first act break are act 1.
    """
    act_breaks = sorted(b.at_beat for b in boundaries if b.kind == "act")
    assignment: dict[int, int] = {}
    for beat in beat_indices:
        act = 1 + sum(1 for brk in act_breaks if beat >= brk)
        assignment[beat] = act
    return assignment


__all__ = [
    "assign_acts_to_beats",
    "detect_act_boundaries",
    "detect_episode_boundaries",
]
