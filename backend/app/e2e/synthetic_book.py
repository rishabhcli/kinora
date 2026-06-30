"""A tiny, deterministic synthetic book — the harness's "book becomes a film" input.

A real Kinora ingest turns a PDF into pages (word boxes), scenes, beats, shots,
and a versioned canon. The end-to-end harness skips PyMuPDF/DashScope ingest and
instead *synthesizes* the same downstream shapes from a fixed prose fixture, so a
scenario can drive the render pipeline + scheduler math over a book that never
changes byte-for-byte between runs.

The fixture is intentionally small (a handful of pages, one voiced character,
one timeline state) but structurally complete: every shot maps to a contiguous
word span on a page, every page carries normalized word boxes, and the canon
slice the render pipeline queries is real (:class:`CanonSlice`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.memory.interfaces import (
    CanonEntitySlice,
    CanonSlice,
    RefImage,
    StateSlice,
)

# --------------------------------------------------------------------------- #
# Identifiers (stable across runs so traces are reproducible)
# --------------------------------------------------------------------------- #

BOOK_ID = "book_synthetic"
SCENE_ID = "scene_001"
CHAR_KEY = "char_mira"
CHAR_VOICE = "vc_mira"
STYLE_KEY = "style_storybook"
STATE_ID = "state_lantern_001"
REF_KEY = f"refs/{BOOK_ID}/{CHAR_KEY}/front.png"
STYLE_REF_KEY = f"refs/{BOOK_ID}/{STYLE_KEY}/key.png"

#: The synthetic prose, one tuple per page: (page_number, [words]). Word indices
#: are assigned globally and contiguously in reading order so a shot span maps
#: cleanly onto a page's boxes.
_PAGES_PROSE: list[tuple[int, str]] = [
    (1, "Mira lifted the lantern and stepped into the quiet hall"),
    (2, "The frost on the window caught the light and held it still"),
    (3, "She climbed the stair and the old house answered with a sigh"),
    (4, "At the top a door stood open onto the cold blue dark"),
]

#: How many source words each beat (and its shot) covers. The walker slices the
#: global word stream into contiguous beats; a beat never straddles a page.
_WORDS_PER_BEAT = 5


# --------------------------------------------------------------------------- #
# Data shapes (plain dataclasses — the harness fakes consume these)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SyntheticPage:
    """One page: its number, full text, and PyMuPDF-style normalized word boxes."""

    page_number: int
    text: str
    word_boxes: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SyntheticBeat:
    """One beat: a contiguous source span + the planner-style summary/visuals."""

    beat_id: str
    scene_id: str
    beat_index: int
    summary: str
    entities: list[str]
    described_visuals: str
    mood: str
    #: ``{"page": int, "word_range": [start, end]}`` — the inclusive global span.
    source_span: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SyntheticShot:
    """One planned shot bound to a beat + the source span it narrates."""

    shot_id: str
    beat_id: str
    scene_id: str
    source_span: dict[str, Any]
    duration_s: float = 5.0


@dataclass(frozen=True, slots=True)
class SyntheticBook:
    """The whole fixture: pages, beats, shots, and the canon slice to render."""

    book_id: str
    pages: list[SyntheticPage]
    beats: list[SyntheticBeat]
    shots: list[SyntheticShot]
    canon_slice: CanonSlice
    #: ``beat_id -> page_number`` so the scheduler can map shots to pages.
    beat_pages: dict[str, int] = field(default_factory=dict)

    def page(self, number: int) -> SyntheticPage | None:
        for page in self.pages:
            if page.page_number == number:
                return page
        return None

    def beat(self, beat_id: str) -> SyntheticBeat | None:
        for beat in self.beats:
            if beat.beat_id == beat_id:
                return beat
        return None

    def shot(self, shot_id: str) -> SyntheticShot | None:
        for shot in self.shots:
            if shot.shot_id == shot_id:
                return shot
        return None

    @property
    def total_words(self) -> int:
        return sum(len(p.word_boxes) for p in self.pages)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _word_boxes_for_page(words: list[str], start_index: int) -> list[dict[str, Any]]:
    """Lay words out left-to-right with deterministic normalized bboxes."""
    boxes: list[dict[str, Any]] = []
    x = 0.08
    y = 0.30
    for offset, text in enumerate(words):
        width = max(0.03, min(0.12, 0.018 * len(text)))
        boxes.append(
            {
                "word_index": start_index + offset,
                "text": text,
                "bbox": [round(x, 4), y, round(width, 4), 0.022],
            }
        )
        x += width + 0.01
        if x > 0.9:  # wrap to the next line, deterministically
            x = 0.08
            y = round(y + 0.05, 4)
    return boxes


def _canon_slice(*, beat_id: str, beat_index: int) -> CanonSlice:
    """The render pipeline's :class:`CanonSlice`: one voiced, locked-ref character."""
    character = CanonEntitySlice(
        entity_key=CHAR_KEY,
        type="character",
        name="Mira",
        version=1,
        description="a steady girl carrying a lantern",
        voice={"cosyvoice_voice_id": CHAR_VOICE},
        reference_images=[RefImage(key=REF_KEY, url=None, pose="front", locked=True)],
        valid_from_beat=1,
    )
    state = StateSlice(
        state_id=STATE_ID,
        subject_entity_key=CHAR_KEY,
        predicate="carries",
        object_value="lantern",
        valid_from_beat=1,
        valid_to_beat=None,
    )
    style = CanonEntitySlice(
        entity_key=STYLE_KEY,
        type="style",
        name="Cool storybook",
        version=1,
        style_tokens={"palette": "cool", "lens": "wide"},
        reference_images=[RefImage(key=STYLE_REF_KEY, url=None, pose="key", locked=True)],
        valid_from_beat=1,
    )
    return CanonSlice(
        book_id=BOOK_ID,
        beat_id=beat_id,
        beat_index=beat_index,
        scene_id=SCENE_ID,
        characters=[character],
        active_states=[state],
        previous_endpoint=None,
        style=style,
    )


def make_synthetic_book(*, book_id: str = BOOK_ID) -> SyntheticBook:
    """Build the deterministic synthetic book fixture (pure; no I/O)."""
    pages: list[SyntheticPage] = []
    global_index = 0
    #: ``page_number -> (first_word_index, [(word_index, text)])``
    page_words: dict[int, list[tuple[int, str]]] = {}
    for page_number, prose in _PAGES_PROSE:
        words = prose.split()
        boxes = _word_boxes_for_page(words, global_index)
        pages.append(
            SyntheticPage(
                page_number=page_number,
                text=prose,
                word_boxes=boxes,
            )
        )
        page_words[page_number] = [(global_index + i, w) for i, w in enumerate(words)]
        global_index += len(words)

    beats: list[SyntheticBeat] = []
    shots: list[SyntheticShot] = []
    beat_pages: dict[str, int] = {}
    beat_index = 0
    moods = ["hushed", "still", "uneasy", "open"]
    for page_number, _prose in _PAGES_PROSE:
        indices = page_words[page_number]
        # Slice this page's words into contiguous beats of <= _WORDS_PER_BEAT.
        for offset in range(0, len(indices), _WORDS_PER_BEAT):
            chunk = indices[offset : offset + _WORDS_PER_BEAT]
            if not chunk:
                continue
            start_word = chunk[0][0]
            end_word = chunk[-1][0]
            summary_words = [w for _, w in chunk]
            beat_id = f"beat_{beat_index:04d}"
            span = {"page": page_number, "word_range": [start_word, end_word]}
            beats.append(
                SyntheticBeat(
                    beat_id=beat_id,
                    scene_id=SCENE_ID,
                    beat_index=beat_index,
                    summary=" ".join(summary_words),
                    entities=[CHAR_KEY],
                    described_visuals=f"Mira on page {page_number}, lantern light",
                    mood=moods[(page_number - 1) % len(moods)],
                    source_span=span,
                )
            )
            shots.append(
                SyntheticShot(
                    shot_id=f"shot_{beat_index:05d}",
                    beat_id=beat_id,
                    scene_id=SCENE_ID,
                    source_span=dict(span),
                    duration_s=5.0,
                )
            )
            beat_pages[beat_id] = page_number
            beat_index += 1

    canon = _canon_slice(beat_id=beats[0].beat_id, beat_index=0)
    return SyntheticBook(
        book_id=book_id,
        pages=pages,
        beats=beats,
        shots=shots,
        canon_slice=canon,
        beat_pages=beat_pages,
    )


__all__ = [
    "BOOK_ID",
    "CHAR_KEY",
    "CHAR_VOICE",
    "REF_KEY",
    "SCENE_ID",
    "STATE_ID",
    "STYLE_KEY",
    "STYLE_REF_KEY",
    "SyntheticBeat",
    "SyntheticBook",
    "SyntheticPage",
    "SyntheticShot",
    "make_synthetic_book",
]
