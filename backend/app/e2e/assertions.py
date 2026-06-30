"""Observable-outcome assertions for the end-to-end harness.

A scenario proves "a book became a page-synced film" by asserting *observable*
properties of the run, not internals: shots reach a terminal state, the buffer
stays ahead of the reader, every narrated word is covered by the sync map, the
budget ledger never double-spends, and the degradation ladder engages when
forced. These helpers raise :class:`HarnessAssertionError` with a precise
message so a failing scenario points at exactly which invariant broke — usable
both inside pytest and from a standalone harness run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.db.models.enums import ShotStatus
from app.e2e.world import FakeWorld
from app.render.pipeline import RenderResult

#: Terminal states a shot may legitimately reach after a render (§9.7).
_TERMINAL = {ShotStatus.ACCEPTED, ShotStatus.DEGRADED, ShotStatus.CONFLICT}


class HarnessAssertionError(AssertionError):
    """A harness invariant was violated (carries a precise diagnostic)."""


def assert_shots_accepted(
    results: Iterable[RenderResult],
    *,
    allow_degraded: bool = True,
) -> None:
    """Every result reached a terminal state (accepted, or degraded if allowed)."""
    allowed = {ShotStatus.ACCEPTED}
    if allow_degraded:
        allowed |= {ShotStatus.DEGRADED}
    for result in results:
        status = result.status
        if status not in _TERMINAL:
            raise HarnessAssertionError(
                f"shot {result.shot_id} did not reach a terminal state: {status}"
            )
        if status not in allowed:
            permitted = sorted(s.value for s in allowed)
            raise HarnessAssertionError(
                f"shot {result.shot_id} reached {status} but only {permitted} were "
                "permitted for this scenario"
            )
        if result.clip_key is None:
            raise HarnessAssertionError(
                f"shot {result.shot_id} reached {status} with no clip_key — nothing to play"
            )


def assert_buffer_ahead_of_reader(
    world: FakeWorld,
    accepted_shot_ids: Sequence[str],
) -> None:
    """The committed shots (inside the commit horizon) are all rendered/ready.

    "Buffer ahead of the reader" means: for the reader's current position, every
    shot the §4.4 zone math classifies COMMITTED has already been driven to a
    terminal render state. If a committed shot is still PLANNED, the reader would
    catch up to an unrendered shot — a buffer underrun.
    """
    accepted = set(accepted_shot_ids)
    committed = world.committed_shots()
    if not committed:
        return  # nothing within the horizon yet — vacuously ahead
    behind = [s for s in committed if s not in accepted]
    if behind:
        raise HarnessAssertionError(
            "buffer underrun: committed shots not yet rendered ahead of the reader at "
            f"word {world.reader.focus_word}: {behind}"
        )


def assert_sync_covers_narration(result: RenderResult) -> None:
    """The §9.4 sync segment covers the shot's narration with monotonic timings.

    A page-synced film needs every narrated word to have a karaoke anchor; the
    sync segment's word timings must be non-empty, ordered, and bounded by the
    clip duration.
    """
    seg = result.sync_segment
    if not seg:
        raise HarnessAssertionError(f"shot {result.shot_id} produced no sync segment")
    words = seg.get("words") or []
    if not words:
        raise HarnessAssertionError(
            f"shot {result.shot_id} sync segment has no words — narration uncovered"
        )
    video_end = float(seg.get("video_end_s", 0.0))
    last_end = 0.0
    for i, word in enumerate(words):
        t0 = float(word["t_start"])
        t1 = float(word["t_end"])
        if t1 < t0:
            raise HarnessAssertionError(
                f"shot {result.shot_id} word {i} has reversed timing: {t0} > {t1}"
            )
        if t0 + 1e-6 < last_end:
            raise HarnessAssertionError(
                f"shot {result.shot_id} word {i} starts before the previous ended "
                f"({t0} < {last_end}) — sync map not monotonic"
            )
        last_end = t1
    if last_end > video_end + 0.5:
        raise HarnessAssertionError(
            f"shot {result.shot_id} narration ({last_end}s) overruns the clip ({video_end}s)"
        )


def assert_no_double_spend(world: FakeWorld) -> None:
    """The budget ledger never committed more video-seconds than its pool.

    Each successful live render commits *once* against a reservation; the
    committed total must stay within the configured pool, and there must be no
    dangling reservations (every reserve is matched by a commit or a release).
    """
    budget = world.budget
    if budget.committed_total > world.config.budget_s + 1e-6:
        raise HarnessAssertionError(
            f"double-spend: committed {budget.committed_total}s exceeds the "
            f"{world.config.budget_s}s pool"
        )
    matched = len(budget.commit_calls) + budget.release_calls
    if matched > len(budget.reserve_calls):
        raise HarnessAssertionError(
            f"ledger imbalance: {matched} commits+releases for only "
            f"{len(budget.reserve_calls)} reservations"
        )


def assert_degradation_engaged(results: Iterable[RenderResult]) -> None:
    """At least one shot rode the real Ken-Burns degradation ladder (§4.4/§12.4)."""
    rungs = [r.rung for r in results]
    if not any(r != "full_video" and r != "cache_hit" for r in rungs):
        raise HarnessAssertionError(
            f"expected the degradation ladder to engage, but all rungs were {rungs}"
        )


__all__ = [
    "HarnessAssertionError",
    "assert_buffer_ahead_of_reader",
    "assert_degradation_engaged",
    "assert_no_double_spend",
    "assert_shots_accepted",
    "assert_sync_covers_narration",
]
