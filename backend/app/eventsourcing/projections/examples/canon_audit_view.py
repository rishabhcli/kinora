"""Example projection: the canon audit view (§7.2 + §8 + §8.5).

§8 makes the canon a bitemporal knowledge graph with an append-only, hash-chained
audit log of every mutation; §7.2 shows continuity conflicts resolved live. This
projection folds canon-mutation events into an **audit view**: one row per
canon subject (an entity / fact key) carrying its current asserted value, the
beat interval it is valid for, whether it has been retired (§8.5 forgetting), and
a compact history of the mutations that produced it.

It is the read model behind the demo's "what did this director edit actually
change" panel and the conflict-resolution feed. Paired with
:mod:`app.eventsourcing.projections.temporal`, an operator can also ask "what did
canon look like as of beat N / write T" by replaying the same projection — this
view is the *current* fold; the as-of projector is the historical one.

Consumed events (stream ``canon:<subject>``):

* ``canon.fact_asserted`` — set/replace the active value + validity interval.
* ``canon.fact_corrected`` — overwrite a fact (records the prior value in history).
* ``canon.fact_retired`` — close the validity interval (§8.5); marks retired.
* ``canon.conflict_resolved`` — records a §7.2 arbitration outcome in history.

Each handler appends a bounded history entry (kept to the most recent
``_MAX_HISTORY``) so the row stays small while still showing the recent edit
trail. Replays reconstruct the identical history because the runtime applies
each event exactly once in position order.
"""

from __future__ import annotations

from typing import Any

from app.eventsourcing.projections.contracts import StoredEvent
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import ReadModelStore

_MAX_HISTORY = 20


class CanonAuditViewProjection(Projection):
    """One row per canon subject: current value + validity + recent mutation trail."""

    name = "canon_audit_view"
    version = 1

    async def _row(self, store: ReadModelStore, namespace: str, key: str) -> dict[str, Any]:
        existing = await store.get(namespace, key)
        if existing is not None:
            return existing.value
        return {
            "subject": key,
            "predicate": None,
            "value": None,
            "valid_from_beat": None,
            "valid_to_beat": None,
            "retired": False,
            "branch": "main",
            "revision": 0,
            "history": [],
        }

    def _append_history(self, row: dict[str, Any], entry: dict[str, Any]) -> None:
        history: list[dict[str, Any]] = list(row["history"])
        history.append(entry)
        # Keep only the most recent N to bound the row size.
        row["history"] = history[-_MAX_HISTORY:]
        row["revision"] = int(row["revision"]) + 1

    @handles("canon.fact_asserted")
    async def _on_asserted(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["predicate"] = event.payload.get("predicate", row["predicate"])
        row["value"] = event.payload.get("value")
        row["valid_from_beat"] = event.payload.get("valid_from_beat")
        row["valid_to_beat"] = event.payload.get("valid_to_beat")
        row["retired"] = False
        row["branch"] = event.payload.get("branch", row["branch"])
        self._append_history(
            row,
            {
                "action": "assert",
                "value": event.payload.get("value"),
                "at": _ts(event),
                "position": event.global_position,
            },
        )
        await store.put(namespace, event.stream_id, row)

    @handles("canon.fact_corrected")
    async def _on_corrected(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._row(store, namespace, event.stream_id)
        prior = row["value"]
        row["value"] = event.payload.get("value")
        if event.payload.get("valid_from_beat") is not None:
            row["valid_from_beat"] = event.payload.get("valid_from_beat")
        if event.payload.get("valid_to_beat") is not None:
            row["valid_to_beat"] = event.payload.get("valid_to_beat")
        self._append_history(
            row,
            {
                "action": "correct",
                "from": prior,
                "value": event.payload.get("value"),
                "reason": event.payload.get("reason"),
                "at": _ts(event),
                "position": event.global_position,
            },
        )
        await store.put(namespace, event.stream_id, row)

    @handles("canon.fact_retired")
    async def _on_retired(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["retired"] = True
        row["valid_to_beat"] = event.payload.get("valid_to_beat", row["valid_to_beat"])
        self._append_history(
            row,
            {
                "action": "retire",
                "valid_to_beat": event.payload.get("valid_to_beat"),
                "at": _ts(event),
                "position": event.global_position,
            },
        )
        await store.put(namespace, event.stream_id, row)

    @handles("canon.conflict_resolved")
    async def _on_conflict(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._row(store, namespace, event.stream_id)
        # Arbitration may pick a winning value (§7.2).
        if "value" in event.payload:
            row["value"] = event.payload.get("value")
        self._append_history(
            row,
            {
                "action": "conflict_resolved",
                "winner": event.payload.get("winner"),
                "value": event.payload.get("value"),
                "at": _ts(event),
                "position": event.global_position,
            },
        )
        await store.put(namespace, event.stream_id, row)


def _ts(event: StoredEvent) -> str | None:
    return event.recorded_at.isoformat() if event.recorded_at is not None else None


__all__ = ["CanonAuditViewProjection"]
