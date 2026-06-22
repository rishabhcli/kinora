"""Shot planning + the §4.2 source-span index — the reconciliation core.

The pure reconciliation tests run everywhere; the persistence/resolution test
runs against the throwaway Postgres and uses a MONKEYPATCHED Adapter (canned
beats whose approximate spans the reconciler must override) so the real thing
being tested is: does ``resolve_word_to_shot`` return the right shot for sampled
word indices once the index is built from the REAL extracted page words.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.adapter import Adapter
from app.agents.contracts import Beat, SourceSpan
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from app.ingest.canon_build import CanonBuildResult, CanonEntity
from app.ingest.pdf_extract import extract_pdf
from app.ingest.shot_plan import (
    BeatSpanInput,
    PageWords,
    group_scenes,
    plan_and_persist,
    reconcile_beat_word_ranges,
)
from app.providers import Providers
from tests.test_ingest_support import (
    MemoryBlobStore,
    build_test_pdf,
    providers,  # noqa: F401  (pytest fixture)
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

# --------------------------------------------------------------------------- #
# Pure reconciliation (no DB, always runs)
# --------------------------------------------------------------------------- #


def test_group_scenes_by_pages() -> None:
    assert [g.scene_id for g in group_scenes([1, 2, 3], pages_per_scene=1)] == [
        "scene_001",
        "scene_002",
        "scene_003",
    ]
    two = group_scenes([1, 2, 3, 4], pages_per_scene=2)
    assert [(g.page_start, g.page_end) for g in two] == [(1, 2), (3, 4)]


def test_reconcile_proportional_split_when_no_phrase_matches() -> None:
    pages = {1: PageWords(start=0, texts=tuple(f"w{i}" for i in range(10)))}
    beats = [BeatSpanInput("b0", 1, "alpha bravo"), BeatSpanInput("b1", 1, "charlie delta")]

    ranges = reconcile_beat_word_ranges(beats, pages)

    # Contiguous, ordered, non-overlapping coverage of the whole page slice.
    assert ranges["b0"][0] == 0
    assert ranges["b1"][1] == 10
    assert ranges["b0"][1] == ranges["b1"][0]
    assert ranges["b0"][1] - ranges["b0"][0] >= 1
    assert ranges["b1"][1] - ranges["b1"][0] >= 1


def test_reconcile_anchors_beats_to_their_phrases() -> None:
    texts = (
        "the", "fox", "ran", "across", "the", "field",
        "then", "the", "owl", "flew", "over", "hill",
    )
    pages = {1: PageWords(start=0, texts=texts)}
    beats = [
        BeatSpanInput("bA", 1, "the fox ran across the field"),
        BeatSpanInput("bB", 1, "the owl flew over the hill"),
    ]

    ranges = reconcile_beat_word_ranges(beats, pages)

    # "fox" (index 1) lands in beat A; "owl" (index 8) lands in beat B.
    a_lo, a_hi = ranges["bA"]
    b_lo, b_hi = ranges["bB"]
    assert a_lo == 0 and a_lo <= 1 < a_hi
    assert b_lo <= 8 < b_hi and b_hi == 12
    assert a_hi == b_lo  # contiguous


def test_reconcile_is_book_global_across_pages() -> None:
    pages = {
        1: PageWords(start=0, texts=("a", "b", "c", "d", "e")),
        2: PageWords(start=5, texts=("f", "g", "h", "i", "j")),
    }
    beats = [BeatSpanInput("b0", 1, "x"), BeatSpanInput("b1", 2, "y")]

    ranges = reconcile_beat_word_ranges(beats, pages)

    # Each page's single beat covers exactly that page's global slice.
    assert ranges["b0"] == (0, 5)
    assert ranges["b1"] == (5, 10)


# --------------------------------------------------------------------------- #
# Persistence + index resolution (real DB, monkeypatched Adapter)
# --------------------------------------------------------------------------- #

pytestmark_db = requires_db

_PAGE_TEXT = [
    "The brave knight rode his strong horse through the dark forest at night.",
    "The gentle princess waited by the silver fountain in the royal palace garden.",
]

# Canned beats per page. The (0, 0) source spans are deliberately wrong — the
# reconciler MUST replace them with real global ranges from the page words.
_CANNED: dict[int, list[tuple[str, str, list[str]]]] = {
    1: [
        ("The knight rode his horse", "a knight on a strong horse", ["knight"]),
        ("Through the dark forest at night", "a dark forest at night", []),
    ],
    2: [
        ("The princess waited by the fountain", "a princess by a silver fountain", ["princess"]),
        ("The royal palace garden", "a royal palace garden", []),
    ],
}


def _fake_adapter(adapter: Adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze_page(
        page_text: str,
        *,
        page: int = 1,
        scene_id: str | None = None,
        beat_index_start: int = 0,
        detected_illustrations: list[str] | None = None,
        known_entities: set[str] | None = None,
        max_tokens: int | None = None,
    ) -> list[Beat]:
        beats: list[Beat] = []
        for offset, (summary, visuals, entities) in enumerate(_CANNED[page]):
            index = beat_index_start + offset
            beats.append(
                Beat(
                    beat_id=f"beat_{index:04d}",
                    beat_index=index,
                    scene_id=scene_id,
                    summary=summary,
                    described_visuals=visuals,
                    entities=entities,
                    source_span=SourceSpan(page=page, word_range=(0, 0)),
                )
            )
        return beats

    monkeypatch.setattr(adapter, "analyze_page", fake_analyze_page)


@pytestmark_db
async def test_shot_plan_persists_and_index_resolves(
    session: AsyncSession,  # noqa: F811
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryBlobStore()
    book = await BookRepo(session).create(title="Reconciliation")
    extract = await extract_pdf(
        PageRepo(session), book_id=book.id, pdf_bytes=build_test_pdf(_PAGE_TEXT), blob_store=store
    )

    canon = CanonBuildResult(
        book_id=book.id,
        style_key="style_book",
        entities=[
            CanonEntity(entity_key="char_knight", kind="character", name="knight"),
            CanonEntity(entity_key="char_princess", kind="character", name="princess"),
        ],
        alias_index={"knight": "char_knight", "princess": "char_princess"},
    )

    adapter = Adapter(providers)
    _fake_adapter(adapter, monkeypatch)

    plan = await plan_and_persist(
        book_id=book.id,
        extract=extract,
        canon=canon,
        adapter=adapter,
        scenes=SceneRepo(session),
        beats=BeatRepo(session),
        shots=ShotRepo(session),
        spans=SourceSpanRepo(session),
        pages_per_scene=1,
    )

    # Scenes / beats / shots persisted.
    assert len(plan.scene_ids) == 2
    assert plan.num_beats == 4
    assert plan.num_shots >= 4
    assert plan.num_spans == plan.num_shots  # every shot has a (non-empty) span row
    assert len(await SceneRepo(session).list_by_book(book.id)) == 2

    # The Adapter's bogus (0,0) spans were overridden with REAL global ranges.
    p2_start = extract.pages[1].word_index_start
    assert plan.beat_ranges["beat_0000"][0] == 0  # first beat of page 1 starts at 0
    assert plan.beat_ranges["beat_0002"][0] == p2_start  # first beat of page 2

    # Beat entity resolution mapped canon names → entity_keys.
    b0 = await BeatRepo(session).get("beat_0000")
    assert b0 is not None and "char_knight" in b0.entities
    b2 = await BeatRepo(session).get("beat_0002")
    assert b2 is not None and "char_princess" in b2.entities

    # THE KEY CHECK: every extracted word resolves to the shot whose reconciled
    # range contains it (contiguous global coverage, O(log n) seek).
    spans = SourceSpanRepo(session)

    def expected_shot(word: int) -> str | None:
        for shot_id, (lo, hi) in plan.shot_ranges.items():
            if lo <= word < hi:
                return shot_id
        return None

    assert extract.total_words > 0
    checked = 0
    for word in range(extract.total_words):
        want = expected_shot(word)
        if want is None:
            continue
        resolved = await spans.resolve_word_to_shot(book.id, word)
        assert resolved is not None and resolved.id == want, (word, want)
        checked += 1
    assert checked == extract.total_words  # full coverage — no gaps

    # A word past the last span still resolves (greatest start ≤ w → last shot).
    tail = await spans.resolve_word_to_shot(book.id, extract.total_words + 50)
    assert tail is not None
