"""The curated public-domain catalogue — the Retrieval/Understanding manifest (WS1).

A deterministic, genre/era-classified set of canonical public-domain books the
zero-spend seeder turns into a real shelf. Built from **Gutendex** (correct ids,
canonical titles/authors, subjects, and the EPUB download URL) over a curated
backbone of must-have classics, then frozen to ``assets/books/catalog.json`` so
re-seeding is idempotent.

The classification + id/url derivation + JSON IO are pure (unit-tested);
:func:`build_from_gutendex` is the only network path (used to (re)generate the
manifest, not at seed time).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The fixed genre vocabulary the shelf groups by.
GENRES = (
    "Classics",
    "Gothic & Horror",
    "Science Fiction",
    "Fantasy",
    "Adventure",
    "Mystery & Detective",
    "Romance",
    "Children's",
    "Poetry",
    "Drama",
    "Philosophy & Essays",
    "Historical",
    "World Literature",
)

#: Ordered (genre, keyword) rules — first hit wins, so the specific beats generic.
_GENRE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Science Fiction", ("science fiction", "sci-fi")),
    ("Gothic & Horror", ("horror", "gothic", "ghost", "vampire", "supernatural")),
    ("Mystery & Detective", ("detective", "mystery", "crime")),
    ("Fantasy", ("fantasy",)),
    ("Adventure", ("adventure", "pirate", "sea stories", "western", "robinsonades")),
    ("Romance", ("love stories", "romance", "courtship")),
    ("Children's", ("fairy tale", "children", "juvenile", "nursery")),
    ("Poetry", ("poetry", "poems", "verse", "sonnet")),
    ("Drama", ("drama", "plays", "tragedy", "comedy", "theater")),
    ("Philosophy & Essays", ("philosophy", "essays", "ethics", "political science")),
    ("Historical", ("historical fiction", "history")),
)


def book_id_for(gid: int) -> str:
    """A stable 32-char Kinora book id per Gutenberg work (matches the repo id width)."""
    return f"pubdom{gid}".ljust(32, "0")[:32]


def gutenberg_epub_url(gid: int) -> str:
    """The Gutenberg cache-mirror EPUB URL (the reliable bulk-download path)."""
    return f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.epub"


def genre_for(subjects: list[str], bookshelves: list[str]) -> str:
    """Classify into a single :data:`GENRES` value from Gutendex subjects/shelves."""
    blob = " ".join([*subjects, *bookshelves]).lower()
    for genre, keywords in _GENRE_RULES:
        if any(kw in blob for kw in keywords):
            return genre
    return "Classics"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def era_for(birth_year: int | None, death_year: int | None) -> str:
    """A human era label from an author's lifespan (best-effort)."""
    year = death_year if death_year is not None else (
        birth_year + 50 if birth_year is not None else None
    )
    if year is None:
        return "Classic"
    if year < 500:
        return "Antiquity"
    if year < 1500:
        return "Medieval"
    century = (year - 1) // 100 + 1
    return f"{_ordinal(century)} century"


@dataclass
class CatalogEntry:
    """One catalogued public-domain book + the metadata the shelf reasons about."""

    gutenberg_id: int
    title: str
    author: str | None = None
    genre: str = "Classics"
    era: str = "Classic"
    tags: list[str] = field(default_factory=list)
    language: str = "en"
    source: str = "gutenberg"
    epub_url: str = ""
    cover_source: str = ""  # filled by fetch_hd_covers: openlibrary|google|generated
    cover_id: int | None = None
    summary: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = book_id_for(self.gutenberg_id)
        if not self.epub_url:
            self.epub_url = gutenberg_epub_url(self.gutenberg_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "gutenberg_id": self.gutenberg_id,
            "title": self.title,
            "author": self.author,
            "genre": self.genre,
            "era": self.era,
            "tags": list(self.tags),
            "language": self.language,
            "source": self.source,
            "epub_url": self.epub_url,
            "cover_source": self.cover_source,
            "cover_id": self.cover_id,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogEntry:
        return cls(
            gutenberg_id=int(data["gutenberg_id"]),
            title=data["title"],
            author=data.get("author"),
            genre=data.get("genre", "Classics"),
            era=data.get("era", "Classic"),
            tags=list(data.get("tags") or []),
            language=data.get("language", "en"),
            source=data.get("source", "gutenberg"),
            epub_url=data.get("epub_url", ""),
            cover_source=data.get("cover_source", ""),
            cover_id=data.get("cover_id"),
            summary=data.get("summary"),
            id=data.get("id", ""),
        )


def dedupe_by_gid(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Keep the first entry per Gutenberg id, preserving order."""
    seen: set[int] = set()
    out: list[CatalogEntry] = []
    for entry in entries:
        if entry.gutenberg_id in seen:
            continue
        seen.add(entry.gutenberg_id)
        out.append(entry)
    return out


def write_catalog(entries: list[CatalogEntry], path: str | Path) -> None:
    """Freeze the catalogue to ``path`` (the idempotent seed manifest)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(entries),
        "books": [e.to_dict() for e in entries],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_catalog(path: str | Path) -> list[CatalogEntry]:
    """Load the catalogue manifest (accepts a bare array or ``{"books": [...]}``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    books = data["books"] if isinstance(data, dict) else data
    return [CatalogEntry.from_dict(b) for b in books]


# --------------------------------------------------------------------------- #
# Catalogue generation (network — used to (re)generate the manifest, not at seed)
# --------------------------------------------------------------------------- #

#: Must-have canonical works, guaranteed in the catalogue (verified via Gutendex).
CURATED_GUTENBERG_IDS = (
    1342, 84, 1661, 2701, 11, 98, 1400, 345, 43, 46, 174, 76, 74, 1260, 768, 158,
    161, 215, 1184, 2641, 2600, 1399, 2554, 1497, 35, 36, 5230, 120, 829, 16, 236,
    1322, 1727, 6130, 996, 1257, 135, 2814, 28054, 1232, 4300, 64317, 25344, 209,
    160, 730, 1080, 2542, 30254, 1952, 5200, 1184, 100, 766, 521, 1259, 244, 2852,
    105, 141, 110, 271, 2680, 3207, 1399, 408, 33, 2009, 1497, 6593, 2680,
)


def _normalize_author(name: str) -> str:
    """``"Austen, Jane"`` → ``"Jane Austen"`` (Gutendex stores surname-first)."""
    if "," in name:
        last, _, first = name.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def _entry_from_gutendex(book: dict[str, Any]) -> CatalogEntry | None:
    """Map a Gutendex book record to a :class:`CatalogEntry` (or ``None`` if unusable)."""
    if book.get("copyright") is True:
        return None
    languages = book.get("languages") or []
    if "en" not in languages:
        return None
    formats = book.get("formats") or {}
    epub_url = ""
    for mime, url in formats.items():
        if mime.startswith("application/epub+zip") and "noimages" not in url:
            epub_url = url
            break
    authors = book.get("authors") or []
    author = _normalize_author(authors[0]["name"]) if authors else None
    birth = authors[0].get("birth_year") if authors else None
    death = authors[0].get("death_year") if authors else None
    gid = int(book["id"])
    subjects = list(book.get("subjects") or [])
    shelves = list(book.get("bookshelves") or [])
    summary_list = book.get("summaries") or []
    summary = summary_list[0] if summary_list else None
    return CatalogEntry(
        gutenberg_id=gid,
        title=str(book.get("title") or "Untitled").strip()[:480],
        author=author,
        genre=genre_for(subjects, shelves),
        era=era_for(birth, death),
        tags=list(subjects[:4]),
        epub_url=epub_url or gutenberg_epub_url(gid),
        summary=(summary[:600] if isinstance(summary, str) else None),
    )


def build_from_gutendex(target: int = 120, *, timeout: float = 30.0) -> list[CatalogEntry]:
    """Assemble ≥``target`` catalogue entries from Gutendex (curated + popular + topics)."""
    import httpx

    base = "https://gutendex.com/books/"
    topics = ("science fiction", "horror", "adventure", "detective", "poetry", "fairy tales")
    out: list[CatalogEntry] = []

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        def collect(url: str) -> str | None:
            resp = client.get(url, headers={"User-Agent": "Kinora/2.0 (+library)"})
            resp.raise_for_status()
            data = resp.json()
            for book in data.get("results", []):
                entry = _entry_from_gutendex(book)
                if entry is not None and entry.epub_url:
                    out.append(entry)
            return data.get("next")

        # 1. Curated backbone (guarantees specific classics; corrects metadata).
        ids = ",".join(str(i) for i in dict.fromkeys(CURATED_GUTENBERG_IDS))
        collect(f"{base}?ids={ids}")
        # 2. Most-popular feed for canonical breadth.
        nxt: str | None = f"{base}?sort=popular&languages=en"
        while nxt and len(dedupe_by_gid(out)) < target:
            nxt = collect(nxt)
        # 3. Topic queries so every shelf has depth.
        for topic in topics:
            collect(f"{base}?topic={topic.replace(' ', '%20')}&languages=en&sort=popular")

    deduped = dedupe_by_gid(out)
    return deduped[:target] if len(deduped) > target else deduped


__all__ = [
    "CURATED_GUTENBERG_IDS",
    "CatalogEntry",
    "GENRES",
    "book_id_for",
    "build_from_gutendex",
    "dedupe_by_gid",
    "era_for",
    "genre_for",
    "gutenberg_epub_url",
    "load_catalog",
    "write_catalog",
]
