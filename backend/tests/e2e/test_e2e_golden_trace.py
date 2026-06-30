"""Golden-trace regression tests — a scenario's event sequence is byte-stable.

The harness's promise is that a fixed book produces the *same* page-synced film
every run. We pin that two ways:

  * **Run-to-run determinism** — the same scenario, run twice, yields an
    identical canonical trace (no wall-clock, no random seeds, no float jitter
    leaks in). This is the strongest signal and needs no checked-in golden.
  * **Checked-in golden** — the observable event-kind backbone of each scenario
    is asserted against an inline golden, so a behavioural change to the §9.7
    flow (an extra render, a skipped page turn, a degraded shot that used to be
    accepted) trips the test.

ffmpeg-gated like the scenario tests; kept to small shot counts.
"""

from __future__ import annotations

import pytest

from app.e2e import scenarios
from app.e2e.world import WorldConfig
from app.render import degrade

pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")


def _fast(**overrides: object) -> WorldConfig:
    """A tight-horizon config so the buffer fill renders only the first 1-2 shots."""
    return WorldConfig(commit_horizon_s=2.0, spec_horizon_s=6.0, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Checked-in golden event-kind backbones (the observable story of each scenario)
# --------------------------------------------------------------------------- #

#: Reader opens to page 1, dwells (one page turn), then the buffer fill renders
#: the two committed shots inside the tight horizon.
GOLDEN_READER_KINDS = ["page_turn", "shot_rendered", "shot_rendered"]

#: Director comment: baseline render, the comment, then the regen.
GOLDEN_DIRECTOR_KINDS = ["shot_rendered", "director_comment", "shot_rendered"]

#: A surfaced §7.2 conflict is a single render that ends in CONFLICT.
GOLDEN_CONFLICT_KINDS = ["shot_rendered"]


async def test_reader_trace_matches_golden_backbone() -> None:
    out = await scenarios.reader_opens_and_turns_pages(pages=1, config=_fast())
    assert out.trace is not None
    assert out.trace.kinds() == GOLDEN_READER_KINDS


async def test_director_trace_matches_golden_backbone() -> None:
    out = await scenarios.director_comments_on_region(config=_fast())
    assert out.trace is not None
    assert out.trace.kinds() == GOLDEN_DIRECTOR_KINDS


async def test_conflict_surface_trace_matches_golden_backbone() -> None:
    out = await scenarios.continuity_conflict_arbitrated(
        showrunner_supported=False, director_present=True
    )
    assert out.trace is not None
    assert out.trace.kinds() == GOLDEN_CONFLICT_KINDS
    # The single recorded event carries the surfaced conflict.
    event = out.trace.events[0]
    assert event.kind == "shot_rendered"
    assert event.data["status"] == "conflict"
    assert event.data["has_conflict"] is True


async def test_reader_scenario_is_run_to_run_deterministic() -> None:
    # Two independent runs of the same scenario must canonicalize identically —
    # the harness leaks no nondeterminism (clock, seed, float jitter).
    a = await scenarios.reader_opens_and_turns_pages(pages=1, config=_fast())
    b = await scenarios.reader_opens_and_turns_pages(pages=1, config=_fast())
    assert a.trace is not None and b.trace is not None
    assert a.trace.canonical() == b.trace.canonical()


async def test_conflict_scenario_is_run_to_run_deterministic() -> None:
    a = await scenarios.continuity_conflict_arbitrated(director_present=True)
    b = await scenarios.continuity_conflict_arbitrated(director_present=True)
    assert a.trace is not None and b.trace is not None
    assert a.trace.canonical() == b.trace.canonical()
