"""High-level scenario drivers — the single place to prove "book → page-synced film".

Each driver assembles a :class:`~app.e2e.world.FakeWorld`, drives the *real*
flow end-to-end, and returns a :class:`ScenarioOutcome` (the render results, the
golden trace, the world). A scenario reads like the product story:

  * :func:`ingest_synthetic_book` — turn the synthetic prose into beats/shots.
  * :func:`reader_opens_and_turns_pages` — the reader opens the book and turns N
    pages; the buffer renders ahead and stays ahead.
  * :func:`director_comments_on_region` — a director note regenerates one shot.
  * :func:`budget_exhausts_mid_read` — the video-second pool runs out mid-read;
    the pipeline degrades instead of double-spending.
  * :func:`provider_fails_over` — the Generator fails its first attempts; the
    repair loop / degradation ladder recovers without crashing.
  * :func:`live_gate_off_degrades` — the live gate is off; every shot rides the
    real Ken-Burns ladder and is still page-synced.

Everything is deterministic and infra-free; ``KINORA_LIVE_VIDEO`` is never
enabled and nothing spends real credits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.contracts import DirectorNote
from app.core.logging import get_logger
from app.e2e.synthetic_book import SyntheticBook, make_synthetic_book
from app.e2e.trace import GoldenTrace
from app.e2e.world import QA_PASS, QA_TIMELINE_FAIL, FakeWorld, WorldConfig
from app.render.pipeline import RenderResult

logger = get_logger("app.e2e.scenarios")


@dataclass(slots=True)
class ScenarioOutcome:
    """What a scenario produced: the world, every render result, and the trace."""

    world: FakeWorld
    results: list[RenderResult] = field(default_factory=list)
    trace: GoldenTrace | None = None
    #: Free-form scenario notes (e.g. the book stats from ingest).
    info: dict[str, Any] = field(default_factory=dict)

    def accepted_shot_ids(self) -> list[str]:
        return [r.shot_id for r in self.results]


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


def ingest_synthetic_book(*, book_id: str | None = None) -> SyntheticBook:
    """ "Ingest" the synthetic book: build its pages/beats/shots/canon (pure).

    This is the harness analogue of Phase-A ingest — it produces the same
    downstream shapes (a book with contiguous beats, every shot bound to a span,
    a versioned canon slice) without PyMuPDF or DashScope. A scenario asserts the
    structure: every shot maps to a real beat + page, spans are contiguous and
    non-overlapping, and the canon carries the voiced character.
    """
    book = make_synthetic_book(book_id=book_id) if book_id else make_synthetic_book()
    logger.info(
        "e2e.ingest",
        book_id=book.book_id,
        pages=len(book.pages),
        beats=len(book.beats),
        shots=len(book.shots),
    )
    return book


# --------------------------------------------------------------------------- #
# Reader opens the book + turns pages (the buffer-ahead happy path)
# --------------------------------------------------------------------------- #


async def reader_opens_and_turns_pages(
    *,
    pages: int = 2,
    config: WorldConfig | None = None,
    book: SyntheticBook | None = None,
) -> ScenarioOutcome:
    """Reader opens the book and turns ``pages`` pages; the buffer renders ahead.

    For each page the reader dwells on, the harness advances the reader's focus
    word to the page's first word, renders every shot whose start sits inside the
    §4.4 commit horizon (the buffer fill), and asserts the buffer never falls
    behind. Returns every render result + the golden trace.
    """
    world = FakeWorld(config=config or WorldConfig(), book=book)
    book = world.book
    outcome = ScenarioOutcome(world=world)
    page_numbers = sorted({p.page_number for p in book.pages})[:pages]

    for page_number in page_numbers:
        # Reader turns to this page: focus on its first word, advance the clock.
        first_word = min(
            int(b.source_span["word_range"][0])
            for b in book.beats
            if book.beat_pages.get(b.beat_id) == page_number
        )
        world.reader.focus_word = first_word
        world.reader.page = page_number
        world.clock.advance(2.0)  # dwell
        world.recorder.record("page_turn", page=page_number, focus_word=first_word)

        # Fill the committed buffer: render every shot inside the commit horizon
        # that has not been rendered yet.
        already = {r.shot_id for r in outcome.results}
        for shot_id in world.committed_shots():
            if shot_id in already:
                continue
            result = await world.render_shot(shot_id)
            outcome.results.append(result)

    outcome.trace = world.recorder.trace()
    return outcome


# --------------------------------------------------------------------------- #
# Director comments on a region → regenerate one shot (§5.4)
# --------------------------------------------------------------------------- #


async def director_comments_on_region(
    *,
    config: WorldConfig | None = None,
) -> ScenarioOutcome:
    """A director comments on a region; the shot is regenerated with the note.

    Renders the first shot once (the baseline), then re-renders it with a
    :class:`DirectorNote` + ``director_present=True`` — the §5.4 region-comment
    regen path. The note nudges the designer (a fresh seed) so the regen is an
    observably distinct render. Returns both results.
    """
    world = FakeWorld(config=config or WorldConfig())
    outcome = ScenarioOutcome(world=world)
    shot = world.book.shots[0]

    baseline = await world.render_shot(shot.shot_id)
    outcome.results.append(baseline)

    note = DirectorNote(
        shot_id=shot.shot_id,
        note="make the lantern light warmer and the hall less empty",
        region_png=None,
    )
    world.recorder.record("director_comment", shot_id=shot.shot_id)
    regen = await world.render_shot(
        shot.shot_id,
        director_notes=[note],
        director_present=True,
    )
    outcome.results.append(regen)
    outcome.info["designer_calls"] = world.designer.calls
    outcome.info["last_notes_seen"] = bool(world.designer.last_notes)
    outcome.trace = world.recorder.trace()
    return outcome


# --------------------------------------------------------------------------- #
# Budget exhausts mid-read → degrade, never double-spend
# --------------------------------------------------------------------------- #


async def budget_exhausts_mid_read(
    *,
    budget_s: float = 5.0,
) -> ScenarioOutcome:
    """The video-second pool runs out partway through; the pipeline degrades.

    With a small budget pool, the first shots render live (spending video-seconds)
    until the budget reads "low", after which every shot rides the real Ken-Burns
    degradation ladder at 0 video-seconds. The ledger must never double-spend.
    """
    # Floor at most of the pool so it reads "low" after a couple of live renders.
    config = WorldConfig(budget_s=budget_s, budget_low_floor_s=budget_s - 6.0)
    world = FakeWorld(config=config)
    outcome = ScenarioOutcome(world=world)

    world.reader.focus_word = 0
    for shot in world.book.shots:
        result = await world.render_shot(shot.shot_id)
        outcome.results.append(result)

    outcome.info["committed_video_seconds"] = world.budget.committed_total
    outcome.trace = world.recorder.trace()
    return outcome


# --------------------------------------------------------------------------- #
# Provider fails over → repair / degrade, never crash
# --------------------------------------------------------------------------- #


async def provider_fails_over(
    *,
    fail_first: int = 1,
) -> ScenarioOutcome:
    """The Generator fails its first ``fail_first`` attempts; the flow recovers.

    The §9.5 repair loop retries on a clean provider error; if retries are
    exhausted the §9.7 machine falls to the real degradation ladder. Either way
    the shot reaches a terminal, playable state — the harness proves the flow is
    crash-proof under provider failure (§4.11).
    """
    # A passing-then-passing Critic so a successful retry would accept; the
    # interest is whether the failover is absorbed without crashing.
    config = WorldConfig(
        critic_metrics=[dict(QA_PASS), dict(QA_PASS), dict(QA_PASS)],
        generator_fail_first=fail_first,
    )
    world = FakeWorld(config=config)
    outcome = ScenarioOutcome(world=world)
    shot = world.book.shots[0]
    world.recorder.record("provider_will_fail", attempts=fail_first)
    result = await world.render_shot(shot.shot_id)
    outcome.results.append(result)
    outcome.info["generator_calls"] = world.generator.calls
    outcome.trace = world.recorder.trace()
    return outcome


# --------------------------------------------------------------------------- #
# Live gate off → the real Ken-Burns ladder, still page-synced
# --------------------------------------------------------------------------- #


async def live_gate_off_degrades(*, pages: int = 1) -> ScenarioOutcome:
    """With the live gate off, every shot rides the real degradation ladder.

    This mirrors the default product posture (``KINORA_LIVE_VIDEO`` off): the
    pipeline never calls the live Generator and instead produces real Ken-Burns
    mp4s muxed with narration — still fully page-synced, at 0 video-seconds.
    """
    config = WorldConfig(live_video=False)
    outcome = await reader_opens_and_turns_pages(pages=pages, config=config)
    return outcome


# --------------------------------------------------------------------------- #
# Conflict flow (§7.2) — surfaced or auto-resolved
# --------------------------------------------------------------------------- #


async def continuity_conflict_arbitrated(
    *,
    showrunner_supported: bool = False,
    director_present: bool = False,
) -> ScenarioOutcome:
    """A timeline-failing take triggers the §7.2 Continuity→Showrunner flow.

    The Critic fails the timeline gate, Continuity confirms a real conflict, and
    the Showrunner arbitrates (honour / evolve / surface) per the real
    ``decide_arbitration`` policy. The harness asserts the shot reaches a
    terminal state carrying either a conflict or a decision record.
    """
    config = WorldConfig(
        critic_metrics=[dict(QA_TIMELINE_FAIL), dict(QA_PASS)],
        continuity_contradicts=True,
        showrunner_supported=showrunner_supported,
    )
    world = FakeWorld(config=config)
    outcome = ScenarioOutcome(world=world)
    shot = world.book.shots[0]
    result = await world.render_shot(shot.shot_id, director_present=director_present)
    outcome.results.append(result)
    outcome.info["showrunner_calls"] = world.showrunner.calls
    outcome.info["continuity_calls"] = world.continuity.calls
    outcome.trace = world.recorder.trace()
    return outcome


__all__ = [
    "ScenarioOutcome",
    "budget_exhausts_mid_read",
    "continuity_conflict_arbitrated",
    "director_comments_on_region",
    "ingest_synthetic_book",
    "live_gate_off_degrades",
    "provider_fails_over",
    "reader_opens_and_turns_pages",
]
