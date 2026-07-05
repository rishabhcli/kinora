"""Canon population — dedup entities across pages into the versioned graph (§9.1 step 3).

From the per-page VL analyses this:

* **deduplicates** entities across pages into stable ``entity_key`` s by name
  normalisation (lower-cased, article/punctuation-stripped) plus alias linking,
  merging the appearance descriptions and union-ing aliases — so the same
  character named on three pages becomes ONE versioned canon node;
* writes every entity to the versioned canon via
  :meth:`app.memory.canon_service.CanonService.upsert_entity`, **from beat 0**
  (characters, locations, props) — ``beat_index`` is 0-based
  (:mod:`app.ingest.shot_plan`), so beat 0 is the book's real first beat;
* creates a **Style** node carrying the book's art-direction / palette / lens
  tokens (default from ``book.art_direction``), which every scene is conditioned
  on so the look is a retrieved constant (§8.1);
* asserts the **initial continuity states** the text establishes (possessions,
  locations) via :meth:`CanonService.assert_state`, valid from beat 0 (§8.5).

It returns a :class:`CanonBuildResult` whose alias index + known-name set are the
seam the Adapter step (:mod:`app.ingest.shot_plan`) uses to resolve a beat's
named entities back to canon ``entity_key`` s.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.db.models.enums import EntityType
from app.ingest.analyze import PageAnalysis
from app.memory.canon_service import CanonService

logger = get_logger("app.ingest.canon_build")

#: The single book-default Style node every scene falls back to (§8.1).
STYLE_ENTITY_KEY = "style_book"
#: Art-direction default when the book row carries none (the plan's locked choice).
DEFAULT_ART_DIRECTION = "painterly storybook"
DEFAULT_PALETTE = "warm, soft, storybook palette"
DEFAULT_LENS = "35mm"

_KIND_PREFIX: dict[str, str] = {"character": "char", "location": "loc", "prop": "prop"}
_KIND_PREFERENCE: dict[str, int] = {"character": 3, "location": 2, "prop": 1}
_ARTICLES = ("the ", "a ", "an ")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Normalise a display name to a comparison key (lower, de-articled, depunct)."""
    text = _NON_ALNUM.sub(" ", name.strip().lower())
    text = _WS.sub(" ", text).strip()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article) :]
            break
    return text.strip()


def _slug(normalized: str) -> str:
    slug = _NON_ALNUM.sub("_", normalized).strip("_")
    return slug or "x"


def entity_key_for(kind: str, name: str) -> str:
    """Build the stable ``entity_key`` for ``name`` of ``kind`` (e.g. ``char_elsa``)."""
    prefix = _KIND_PREFIX.get(kind, "char")
    return f"{prefix}_{_slug(normalize_name(name))}"


class CanonEntity(BaseModel):
    """A deduplicated canon entity ready to persist / lock."""

    model_config = ConfigDict(extra="forbid")

    entity_key: str
    #: ``"character"`` / ``"location"`` / ``"prop"`` (the value of an EntityType).
    kind: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    first_page: int = 1
    #: Number of distinct pages the entity was found on (a coarse prominence hint).
    page_count: int = 1

    @property
    def is_character(self) -> bool:
        """Whether this entity is a character (the only kind identity-locked)."""
        return self.kind == "character"


class CanonBuildResult(BaseModel):
    """The outcome of canon population, consumed by shot-plan + identity-lock."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    style_key: str
    entities: list[CanonEntity] = Field(default_factory=list)
    #: normalised name/alias -> entity_key (Adapter beat-entity resolution).
    alias_index: dict[str, str] = Field(default_factory=dict)
    num_states: int = 0

    @property
    def known_names(self) -> set[str]:
        """Every display name + alias (for the Adapter's ``known_entities`` filter)."""
        names: set[str] = set()
        for entity in self.entities:
            names.add(entity.name)
            names.update(entity.aliases)
        return names

    def characters(self) -> list[CanonEntity]:
        """The character entities (identity-lock candidates)."""
        return [e for e in self.entities if e.is_character]


class _Aggregate:
    """Mutable accumulator while deduplicating one entity across pages."""

    def __init__(self, entity_key: str, kind: str, name: str, page: int) -> None:
        self.entity_key = entity_key
        self.kind = kind
        self.name = name
        self.aliases: set[str] = set()
        self.description = ""
        self.first_page = page
        self.pages: set[int] = {page}

    def merge(self, *, name: str, appearance: str, aliases: list[str], page: int) -> None:
        self.pages.add(page)
        self.first_page = min(self.first_page, page)
        # Keep the most detailed appearance description seen.
        if len(appearance.strip()) > len(self.description):
            self.description = appearance.strip()
        # Any name variant beyond the canonical one becomes an alias.
        if normalize_name(name) != normalize_name(self.name):
            self.aliases.add(name.strip())
        for alias in aliases:
            if alias.strip() and normalize_name(alias) != normalize_name(self.name):
                self.aliases.add(alias.strip())


def _dedup_entities(analyses: list[PageAnalysis]) -> tuple[list[_Aggregate], dict[str, str]]:
    """Collapse per-page entities into stable keys; return aggregates + alias index."""
    by_key: dict[str, _Aggregate] = {}
    alias_index: dict[str, str] = {}
    alias_kind: dict[str, str] = {}

    def link(norm: str, key: str, kind: str) -> None:
        # Prefer the higher-priority kind when a normalised name is ambiguous.
        if norm not in alias_index or _KIND_PREFERENCE.get(kind, 0) > _KIND_PREFERENCE.get(
            alias_kind.get(norm, ""), 0
        ):
            alias_index[norm] = key
            alias_kind[norm] = kind

    for analysis in analyses:
        for ent in analysis.entities:
            if not ent.name.strip():
                continue
            norm = normalize_name(ent.name)
            if not norm:
                continue
            # Resolve to an existing entity if this name/alias was already seen.
            key = alias_index.get(norm) or entity_key_for(ent.kind, ent.name)
            agg = by_key.get(key)
            if agg is None:
                agg = _Aggregate(key, ent.kind, ent.name.strip(), analysis.page_number)
                by_key[key] = agg
            agg.merge(
                name=ent.name,
                appearance=ent.appearance,
                aliases=ent.aliases,
                page=analysis.page_number,
            )
            link(norm, key, agg.kind)
            for alias in ent.aliases:
                alias_norm = normalize_name(alias)
                if alias_norm:
                    link(alias_norm, key, agg.kind)
    return list(by_key.values()), alias_index


def _dedup_states(
    analyses: list[PageAnalysis], alias_index: dict[str, str]
) -> list[tuple[str, str, str, int]]:
    """Resolve + dedup establishing facts → ``(subject_key, predicate, object, page)``."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str, int]] = []
    for analysis in analyses:
        for state in analysis.states:
            subject_key = alias_index.get(normalize_name(state.subject))
            if subject_key is None:
                continue  # never assert a fact about an unknown subject
            # Resolve the object to a canon key when it names a known entity.
            object_value = alias_index.get(normalize_name(state.object), state.object.strip())
            predicate = state.predicate.strip().lower() or "is"
            dedup_key = (subject_key, predicate, object_value)
            if not object_value or dedup_key in seen:
                continue
            seen.add(dedup_key)
            out.append((subject_key, predicate, object_value, analysis.page_number))
    return out


async def build_canon(
    canon: CanonService,
    *,
    book_id: str,
    analyses: list[PageAnalysis],
    art_direction: str | None = None,
) -> CanonBuildResult:
    """Deduplicate the analyses into the versioned canon + Style node + initial states.

    Args:
        canon: a :class:`CanonService` bound to the active unit-of-work.
        book_id: the book being populated.
        analyses: the per-page VL analyses.
        art_direction: the book's art direction (defaults to the storybook look).

    Returns:
        The canon build result (entities, alias index, style key, state count).
    """
    aggregates, alias_index = _dedup_entities(analyses)

    entities: list[CanonEntity] = []
    for agg in sorted(aggregates, key=lambda a: (a.first_page, a.entity_key)):
        await canon.upsert_entity(
            book_id=book_id,
            entity_key=agg.entity_key,
            entity_type=EntityType(agg.kind),
            name=agg.name,
            valid_from_beat=0,
            aliases=sorted(agg.aliases) or None,
            description=agg.description or None,
            appearance={"description": agg.description, "locked": False},
            first_appearance={"page": agg.first_page},
        )
        entities.append(
            CanonEntity(
                entity_key=agg.entity_key,
                kind=agg.kind,
                name=agg.name,
                aliases=sorted(agg.aliases),
                description=agg.description,
                first_page=agg.first_page,
                page_count=len(agg.pages),
            )
        )

    # The book-default Style node (palette / lens / art-direction tokens, §8.1).
    art = (art_direction or DEFAULT_ART_DIRECTION).strip() or DEFAULT_ART_DIRECTION
    await canon.upsert_entity(
        book_id=book_id,
        entity_key=STYLE_ENTITY_KEY,
        entity_type=EntityType.STYLE,
        name="Storybook style",
        valid_from_beat=0,
        description=art,
        style_tokens={"art_direction": art, "palette": DEFAULT_PALETTE, "lens": DEFAULT_LENS},
    )

    # Initial continuity states (versioned from beat 0, §8.5).
    states = _dedup_states(analyses, alias_index)
    for subject_key, predicate, object_value, page in states:
        await canon.assert_state(
            book_id=book_id,
            subject_entity_key=subject_key,
            predicate=predicate,
            object_value=object_value,
            valid_from_beat=0,
            source_span={"page": page},
        )

    result = CanonBuildResult(
        book_id=book_id,
        style_key=STYLE_ENTITY_KEY,
        entities=entities,
        alias_index=alias_index,
        num_states=len(states),
    )
    logger.info(
        "ingest.canon.done",
        book_id=book_id,
        entities=len(entities),
        characters=len(result.characters()),
        states=len(states),
    )
    return result


__all__ = [
    "DEFAULT_ART_DIRECTION",
    "STYLE_ENTITY_KEY",
    "CanonBuildResult",
    "CanonEntity",
    "build_canon",
    "entity_key_for",
    "normalize_name",
]
