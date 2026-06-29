"""The canon-graph projection — a multi-source, version-aware materialised view.

Where the library shelf is a 1:1 projection, the canon graph is the harder,
more interesting case: it denormalises **two** source tables (``entities`` and
``continuity_states``) into a single read model, and it must honour the
versioning semantics of kinora.md §8.1 / §8.5:

* ``entities`` rows are *versions* of a logical entity keyed by
  ``(book_id, entity_key)``. The graph node for a key reflects the **present**
  version — the highest ``version`` whose validity interval is open
  (``valid_to_beat is None``). Superseded versions drop out of the projection
  (the §8.5 "forgetting" rule).
* ``continuity_states`` are versioned facts (edges) true over a beat interval. A
  fact with ``valid_to_beat is None`` is *active* and projects as an edge; a
  retired fact (``retire_state`` set its ``valid_to_beat``) drops out.

The view keeps just enough per-key bookkeeping to apply the present-version rule
incrementally: it remembers every live version per ``entity_key`` so that when
the present version is retired or a new version supersedes it, the node can be
recomputed from the remaining versions without a full rescan. ``recompute``
re-derives the same thing from scratch for the consistency oracle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.streaming.cdc.events import ChangeEvent, key_str
from app.streaming.cdc.views.delta import Delta, Row, ZSet, update_delta
from app.streaming.cdc.views.view import MaterializedView


class CanonGraphView(MaterializedView):
    """Active canon nodes (present entity versions) + active edges (open facts)."""

    name = "canon_graph"

    def __init__(self) -> None:
        self._state = ZSet()
        # (book_id, entity_key) -> {version: row}  — every live entity version
        self._entity_versions: dict[str, dict[int, Mapping[str, Any]]] = {}
        # the node Row currently asserted per entity key (for retraction)
        self._node_row: dict[str, Row] = {}
        # continuity_state id -> the edge Row currently asserted (active facts)
        self._edge_row: dict[str, Row] = {}

    @property
    def sources(self) -> tuple[str, ...]:
        return ("entities", "continuity_states")

    @property
    def state(self) -> ZSet:
        return self._state

    # -- incremental maintenance ------------------------------------------- #
    def on_event(self, event: ChangeEvent) -> Delta:
        if not event.is_row_event:
            return ZSet()
        if event.table == "entities":
            return self._on_entity(event)
        if event.table == "continuity_states":
            return self._on_state(event)
        return ZSet()

    def _entity_key_id(self, row: Mapping[str, Any]) -> str:
        return key_str({"book_id": row.get("book_id"), "entity_key": row.get("entity_key")})

    def _on_entity(self, event: ChangeEvent) -> Delta:
        row = event.row or {}
        ekid = self._entity_key_id(row)
        version = int(row.get("version", 1))
        versions = self._entity_versions.setdefault(ekid, {})

        if event.is_delete:
            versions.pop(version, None)
        else:
            versions[version] = dict(row)
        if not versions:
            self._entity_versions.pop(ekid, None)

        new_node = self._present_node(ekid)
        old_node = self._node_row.get(ekid)
        if new_node is None:
            self._node_row.pop(ekid, None)
        else:
            self._node_row[ekid] = new_node
        return update_delta(old_node, new_node)

    def _present_node(self, ekid: str) -> Row | None:
        """The graph node for the present version of this entity key, if any."""
        versions = self._entity_versions.get(ekid, {})
        # Present = open-interval version with the highest version number; if no
        # open version exists the entity has been fully superseded/retired.
        open_versions = [(v, r) for v, r in versions.items() if r.get("valid_to_beat") is None]
        if not open_versions:
            return None
        version, row = max(open_versions, key=lambda kv: kv[0])
        return Row(
            {
                "kind": "node",
                "book_id": row.get("book_id"),
                "entity_key": row.get("entity_key"),
                "entity_type": row.get("type"),
                "name": row.get("name"),
                "version": version,
            }
        )

    def _on_state(self, event: ChangeEvent) -> Delta:
        row = event.row or {}
        state_id = str(event.key.get("id"))
        old_edge = self._edge_row.get(state_id)

        active = (not event.is_delete) and row.get("valid_to_beat") is None
        new_edge = self._edge_from(row) if active else None
        if new_edge is None:
            self._edge_row.pop(state_id, None)
        else:
            self._edge_row[state_id] = new_edge
        return update_delta(old_edge, new_edge)

    @staticmethod
    def _edge_from(row: Mapping[str, Any]) -> Row:
        return Row(
            {
                "kind": "edge",
                "book_id": row.get("book_id"),
                "subject": row.get("subject"),
                "predicate": row.get("predicate"),
                "object": row.get("object"),
                "valid_from_beat": row.get("valid_from_beat"),
            }
        )

    # -- consistency oracle ------------------------------------------------- #
    def recompute(self, base: Mapping[str, Iterable[Mapping[str, Any]]]) -> ZSet:
        out = ZSet()
        # Nodes: group entities by (book, key), keep the present open version.
        by_key: dict[str, dict[int, Mapping[str, Any]]] = {}
        for row in base.get("entities", []):
            ekid = self._entity_key_id(row)
            by_key.setdefault(ekid, {})[int(row.get("version", 1))] = row
        for ekid, versions in by_key.items():
            saved = self._entity_versions
            self._entity_versions = {ekid: versions}
            node = self._present_node(ekid)
            self._entity_versions = saved
            if node is not None:
                out.add(node, +1)
        # Edges: active (open-interval) continuity facts.
        for row in base.get("continuity_states", []):
            if row.get("valid_to_beat") is None:
                out.add(self._edge_from(row), +1)
        return out


__all__ = ["CanonGraphView"]
