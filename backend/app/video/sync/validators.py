"""Validators for a sync map's invariants (§9.4 correctness guarantees).

A sync map drives a *moving highlight on real text*, so the timeline has to be sane
or the karaoke jumps, overlaps, or runs past the clip. These checks express the
three guarantees the §9.4 map must hold:

* **monotonic, non-overlapping** — each word starts at/after the previous word's
  end and ``t_start <= t_end`` (a moving highlight can't go backwards or double up);
* **within duration** — every word lies inside ``[0, video_end_s]`` (the highlight
  never paints past the clip);
* **full coverage** — every spoken word produced a span (no word is silently lost
  between ingest and output).

Each check returns a list of human-readable problem strings (empty == valid).
:func:`validate_sync_map` runs them all; :func:`assert_valid_sync_map` raises.
Pure and deterministic — used both in tests and as a render-time guard.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import SyncMap, WordTiming, coerce_words

#: Float tolerance for the timeline comparisons (timings are rounded to 1e-3).
_EPS = 1e-6


def check_monotonic(words: Sequence[WordTiming], *, eps: float = _EPS) -> list[str]:
    """Each word's span is forward (``t_start <= t_end``) and non-overlapping."""
    problems: list[str] = []
    prev_end = 0.0
    for i, w in enumerate(words):
        if w.t_end + eps < w.t_start:
            problems.append(f"word[{i}] {w.text!r}: t_end {w.t_end} < t_start {w.t_start}")
        if w.t_start + eps < prev_end:
            problems.append(
                f"word[{i}] {w.text!r}: t_start {w.t_start} overlaps prior end {prev_end}"
            )
        prev_end = max(prev_end, w.t_end)
    return problems


def check_within_duration(
    words: Sequence[WordTiming], *, duration_s: float, eps: float = _EPS
) -> list[str]:
    """Every word lies inside ``[0, duration_s]`` (nothing paints past the clip)."""
    problems: list[str] = []
    for i, w in enumerate(words):
        if w.t_start < -eps:
            problems.append(f"word[{i}] {w.text!r}: t_start {w.t_start} < 0")
        if w.t_end > duration_s + eps:
            problems.append(f"word[{i}] {w.text!r}: t_end {w.t_end} > duration {duration_s}")
    return problems


def check_coverage(words: Sequence[WordTiming], *, expected_count: int) -> list[str]:
    """Every spoken word produced exactly one span (no word lost in normalization)."""
    if expected_count < 0:
        return []
    got = len(words)
    if got != expected_count:
        return [f"coverage: produced {got} word spans, expected {expected_count}"]
    return []


def validate_word_timeline(
    words: Sequence[WordTiming],
    *,
    duration_s: float | None = None,
    expected_count: int | None = None,
    eps: float = _EPS,
) -> list[str]:
    """Run every applicable check over a bare :class:`WordTiming` timeline."""
    items = coerce_words(words)
    problems = check_monotonic(items, eps=eps)
    if duration_s is not None:
        problems += check_within_duration(items, duration_s=duration_s, eps=eps)
    if expected_count is not None:
        problems += check_coverage(items, expected_count=expected_count)
    return problems


def validate_sync_map(
    sync_map: SyncMap,
    *,
    expected_word_count: int | None = None,
    eps: float = _EPS,
) -> list[str]:
    """Validate an assembled :class:`SyncMap`: words, page-turn, sentence anchors.

    Beyond the word-timeline checks, asserts ``video_start_s <= page_turn_at_s <
    video_end_s`` (the page must flip during, and before the end of, the shot) and
    that each sentence anchor's ``word_start``/``word_end`` index a real, ordered
    slice of the words. Returns an empty list when the map is valid.
    """
    words = sync_map.words
    timings = [
        WordTiming(text=w.text, t_start=w.t_start, t_end=w.t_end, estimated=w.estimated)
        for w in words
    ]
    problems = check_monotonic(timings, eps=eps)
    problems += check_within_duration(timings, duration_s=sync_map.video_end_s, eps=eps)
    if expected_word_count is not None:
        problems += check_coverage(timings, expected_count=expected_word_count)

    # page_turn_at_s must fall inside the shot window (and strictly before the end
    # for any positive-duration shot, so the next page is settled in time).
    if sync_map.duration_s > eps:
        if not (sync_map.video_start_s - eps <= sync_map.page_turn_at_s):
            problems.append(
                f"page_turn_at_s {sync_map.page_turn_at_s} before "
                f"video_start_s {sync_map.video_start_s}"
            )
        if not (sync_map.page_turn_at_s < sync_map.video_end_s + eps):
            problems.append(
                f"page_turn_at_s {sync_map.page_turn_at_s} not before "
                f"video_end_s {sync_map.video_end_s}"
            )

    n = len(words)
    for i, sent in enumerate(sync_map.sentences):
        if not (0 <= sent.word_start <= sent.word_end < n):
            problems.append(
                f"sentence[{i}] indices [{sent.word_start},{sent.word_end}] "
                f"out of range for {n} words"
            )
    return problems


def assert_valid_sync_map(
    sync_map: SyncMap,
    *,
    expected_word_count: int | None = None,
    eps: float = _EPS,
) -> None:
    """Raise :class:`ValueError` with all problems if ``sync_map`` is invalid."""
    problems = validate_sync_map(sync_map, expected_word_count=expected_word_count, eps=eps)
    if problems:
        raise ValueError("invalid sync map:\n  - " + "\n  - ".join(problems))


__all__ = [
    "assert_valid_sync_map",
    "check_coverage",
    "check_monotonic",
    "check_within_duration",
    "validate_sync_map",
    "validate_word_timeline",
]
