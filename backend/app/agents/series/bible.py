"""Building & querying the cross-book SERIES BIBLE (§7, §8.1).

The series bible is a thin *index* over the per-book canon (§8.1) — it references
entity keys, never duplicates appearance/state. This module assembles a
:class:`~app.agents.contracts.SeriesBible` from volumes + arc beats, and provides
the read queries the Showrunner (and, later, the Cinematographer via an MCP tool)
use against it: which volumes an entity spans, the resolved arc-state of a
character at a series position, the relationships an entity is in, the motifs due
at a position.

All pure. The bible is a read model; persistence (M2) lives in the memory domain.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.contracts import (
    ArcBeat,
    ArcState,
    CharacterArc,
    Motif,
    MotifCallback,
    RelationshipArc,
    RelationshipKind,
    SeriesBible,
    Volume,
)
from app.agents.series.arcs import (
    arc_state_at,
    build_character_arc,
    build_relationship_arc,
    merge_arc_beats,
)
from app.agents.series.motifs import due_callbacks, plan_all_callbacks


def build_series_bible(
    *,
    series_id: str,
    title: str = "",
    volumes: Sequence[Volume],
    character_beats: dict[str, list[ArcBeat]] | None = None,
    character_names: dict[str, str] | None = None,
    relationship_beats: dict[tuple[str, str], list[ArcBeat]] | None = None,
    relationship_kinds: dict[tuple[str, str], RelationshipKind] | None = None,
    motifs: Sequence[Motif] = (),
    synopsis: str = "",
) -> SeriesBible:
    """Assemble a :class:`SeriesBible` from volumes, arc beats, and motifs (§7).

    ``character_beats`` maps an entity key to its sampled arc beats;
    ``relationship_beats`` maps an (unordered) entity pair to its beats. Arcs are
    built (and beats sorted) via the :mod:`app.agents.series.arcs` builders so the
    bible is always well-formed.
    """
    character_beats = character_beats or {}
    character_names = character_names or {}
    relationship_beats = relationship_beats or {}
    relationship_kinds = relationship_kinds or {}

    character_arcs = [
        build_character_arc(
            entity_key=key,
            name=character_names.get(key, ""),
            beats=beats,
        )
        for key, beats in sorted(character_beats.items())
    ]

    relationship_arcs = [
        build_relationship_arc(
            entity_a=pair[0],
            entity_b=pair[1],
            kind=relationship_kinds.get(pair, RelationshipKind.NEUTRAL),
            beats=beats,
        )
        for pair, beats in sorted(relationship_beats.items())
    ]

    return SeriesBible(
        series_id=series_id,
        title=title,
        volumes=sorted(volumes, key=lambda v: v.volume_index),
        character_arcs=character_arcs,
        relationship_arcs=relationship_arcs,
        motifs=list(motifs),
        synopsis=synopsis,
    )


def volume_for_beat(bible: SeriesBible, *, volume_index: int) -> Volume | None:
    """The volume record at a given index, or ``None``."""
    return next((v for v in bible.volumes if v.volume_index == volume_index), None)


def volume_beat_counts(bible: SeriesBible) -> dict[int, int]:
    """A ``{volume_index: beat_count}`` map for motif/structure scheduling."""
    return {v.volume_index: v.beat_count for v in bible.volumes}


def character_arc(bible: SeriesBible, entity_key: str) -> CharacterArc | None:
    """The arc for an entity key, or ``None`` if not tracked."""
    return next((a for a in bible.character_arcs if a.entity_key == entity_key), None)


def character_arc_state_at(
    bible: SeriesBible,
    *,
    entity_key: str,
    volume_index: int,
    beat_index: int,
) -> ArcState | None:
    """Resolve a character's arc-state as of a series position (§8.5 time-travel)."""
    arc = character_arc(bible, entity_key)
    if arc is None:
        return None
    return arc_state_at(arc, volume_index=volume_index, beat_index=beat_index)


def relationships_of(bible: SeriesBible, entity_key: str) -> list[RelationshipArc]:
    """Every relationship arc the entity participates in."""
    return [a for a in bible.relationship_arcs if entity_key in a.entity_keys]


def entities_in_volume(bible: SeriesBible, volume_index: int) -> list[str]:
    """The entity keys whose arcs touch a given volume."""
    keys = {
        a.entity_key for a in bible.character_arcs if volume_index in a.spanned_volumes
    }
    return sorted(keys)


def motif_callbacks(bible: SeriesBible, *, echoes_per_volume: int = 1) -> list[MotifCallback]:
    """Schedule every motif's callbacks against the bible's volume sizes (§7)."""
    return plan_all_callbacks(
        bible.motifs,
        volume_beat_counts=volume_beat_counts(bible),
        echoes_per_volume=echoes_per_volume,
    )


def motifs_due_at(
    bible: SeriesBible,
    *,
    volume_index: int,
    beat_index: int,
    window: int = 0,
    echoes_per_volume: int = 1,
) -> list[MotifCallback]:
    """The motif callbacks due at a series position — the Cinematographer's hook (§7)."""
    return due_callbacks(
        motif_callbacks(bible, echoes_per_volume=echoes_per_volume),
        volume_index=volume_index,
        beat_index=beat_index,
        window=window,
    )


def merge_into_bible(
    bible: SeriesBible,
    *,
    entity_key: str,
    new_beats: Iterable[ArcBeat],
) -> SeriesBible:
    """Return a new bible with ``new_beats`` merged into a character's arc (idempotent).

    Used when a new volume is ingested: its arc samples fold into the existing arc
    without re-deriving the whole bible. De-dups on series position (§8.7-style).
    """
    arcs: list[CharacterArc] = []
    found = False
    for arc in bible.character_arcs:
        if arc.entity_key == entity_key:
            found = True
            merged = merge_arc_beats(arc.beats, new_beats)
            arcs.append(
                build_character_arc(entity_key=entity_key, name=arc.name, beats=merged)
            )
        else:
            arcs.append(arc)
    if not found:
        arcs.append(build_character_arc(entity_key=entity_key, beats=list(new_beats)))
    return bible.model_copy(update={"character_arcs": arcs})


__all__ = [
    "build_series_bible",
    "character_arc",
    "character_arc_state_at",
    "entities_in_volume",
    "merge_into_bible",
    "motif_callbacks",
    "motifs_due_at",
    "relationships_of",
    "volume_beat_counts",
    "volume_for_beat",
]
