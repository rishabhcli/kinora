"""The injectable source/sink seams the DR engine speaks to.

The engine never imports a concrete store. It captures from, and restores into,
five protocols — each with a deterministic in-memory fake here so the whole
backup/restore suite runs with **no infra**:

* :class:`EventSource` — read the append-only log: its head ``global_position``,
  the timestamp of an event at a position (for timestamp→position PITR), and a
  replay of events in a position range. This is the *backup* read side. It maps
  structurally onto the real read-side store
  (:class:`app.eventsourcing.projections.contracts.EventStore`).
* :class:`EventSink` — write events back in order during restore (a clean target
  log). Separate from the source so a dry-run can omit it entirely.
* :class:`CanonSource` — dump/load the canon (versioned entities + episodic shot
  records, §8) as a JSON-able snapshot, and report asset keys it references.
* :class:`ReadModelTarget` — clear + bulk-load read-model rows during a rebuild.
  Structurally a subset of :class:`app.eventsourcing.projections.readmodel`'s
  store with an added bulk dump.
* :class:`AssetSource` — the object-store seam: does a key exist, what is its
  content digest, how big is it, and enumerate keys. Used to *build* the asset
  manifest at capture and to *verify* asset presence/integrity on restore. It
  never copies bytes into the backup — assets stay in object storage.
* :class:`BackupRepository` — where snapshots are persisted (and listed, fetched,
  deleted). The GC + chain walks go through this.

Every fake holds state in plain dicts/lists; no clock, no RNG, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from app.dr.checksums import digest
from app.dr.models import BackupManifest


@runtime_checkable
class EventSource(Protocol):
    """Backup read side of the event log: positions, timestamps, replay."""

    async def head_position(self) -> int:
        """The current maximum ``global_position`` (0 if the log is empty)."""
        ...

    async def position_at_or_before(self, timestamp: float) -> int:
        """Highest position whose event ``recorded_at`` ≤ ``timestamp`` (epoch s).

        0 when no event is at/before ``timestamp``. Drives timestamp→position
        point-in-time resolution.
        """
        ...

    async def read_range(self, after: int, through: int) -> list[dict[str, Any]]:
        """Events with ``after < global_position <= through``, in position order.

        Each event is a JSON-able dict (the serialised :class:`StoredEvent`).
        """
        ...


@runtime_checkable
class EventSink(Protocol):
    """Restore write side: append a replayed event in position order."""

    async def restore_event(self, event: dict[str, Any]) -> None:
        """Append one replayed event to the target log (preserving its position)."""
        ...

    async def reset(self) -> None:
        """Clear the target log (a clean slate before a chain replay)."""
        ...


@runtime_checkable
class CanonSource(Protocol):
    """Dump/load the canon graph + episodic store as a JSON snapshot (§8)."""

    async def dump(self) -> dict[str, Any]:
        """Return the full canon as a JSON-able mapping (entities + episodic)."""
        ...

    async def load(self, snapshot: dict[str, Any]) -> None:
        """Replace the canon with ``snapshot`` (a restore)."""
        ...

    async def asset_keys(self) -> list[str]:
        """Object-store keys the current canon references (refs, episodic clips)."""
        ...


@runtime_checkable
class ReadModelTarget(Protocol):
    """Bulk dump + clear + load of materialised read-model rows."""

    async def dump(self) -> dict[str, list[dict[str, Any]]]:
        """Return ``{namespace: [row, ...]}`` for every read-model namespace."""
        ...

    async def clear_all(self) -> None:
        """Drop every read-model row (before a projection rebuild)."""
        ...

    async def load(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        """Bulk-load ``{namespace: [row, ...]}`` (a direct read-model restore)."""
        ...


@runtime_checkable
class AssetSource(Protocol):
    """The object-store seam used to build + verify the asset manifest."""

    async def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""
        ...

    async def content_digest(self, key: str) -> str | None:
        """The asset's content SHA-256 hex digest, or ``None`` if absent."""
        ...

    async def size(self, key: str) -> int:
        """The asset's byte size (0 if absent)."""
        ...

    async def list_keys(self, prefix: str = "") -> list[str]:
        """Every key under ``prefix`` (``""`` == all)."""
        ...


@runtime_checkable
class BackupRepository(Protocol):
    """Where snapshots are persisted, listed, fetched, and deleted."""

    async def save(self, manifest: BackupManifest) -> None:
        """Persist a backup manifest (idempotent on ``snapshot_id``)."""
        ...

    async def get(self, snapshot_id: str) -> BackupManifest | None:
        """Fetch a manifest by id, or ``None`` if absent."""
        ...

    async def list_ids(self) -> list[str]:
        """Every stored snapshot id (order unspecified)."""
        ...

    async def delete(self, snapshot_id: str) -> bool:
        """Delete a snapshot; return whether it existed."""
        ...


# --------------------------------------------------------------------------- #
# Deterministic in-memory fakes — the test substrate (no infra, no clock).    #
# --------------------------------------------------------------------------- #


class InMemoryEventSource:
    """A position-ordered event log fronting both :class:`EventSource` reads.

    Events are ``{"global_position", "stream_id", "type", "payload", ...}`` dicts;
    ``recorded_at`` (epoch seconds, float) drives timestamp PITR. Positions are
    assigned densely from 1 on append so a test builds a log declaratively.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(
        self,
        stream_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        recorded_at: float | None = None,
    ) -> dict[str, Any]:
        """Append one event, assigning the next gap-free ``global_position``."""
        position = len(self._events) + 1
        event = {
            "global_position": position,
            "stream_id": stream_id,
            "stream_version": position - 1,
            "type": type,
            "payload": dict(payload or {}),
            "recorded_at": float(position) if recorded_at is None else float(recorded_at),
            "event_id": f"evt_{position:012d}",
            "metadata": {},
        }
        self._events.append(event)
        return event

    async def head_position(self) -> int:
        return len(self._events)

    async def position_at_or_before(self, timestamp: float) -> int:
        best = 0
        for e in self._events:
            if float(e["recorded_at"]) <= timestamp:
                best = int(e["global_position"])
            else:
                break
        return best

    async def read_range(self, after: int, through: int) -> list[dict[str, Any]]:
        return [dict(e) for e in self._events if after < int(e["global_position"]) <= through]


class InMemoryEventSink:
    """A clean target log restore replays into (positions preserved)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def restore_event(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    async def reset(self) -> None:
        self.events.clear()

    @property
    def head_position(self) -> int:
        """The highest restored position (0 if empty)."""
        if not self.events:
            return 0
        return max(int(e["global_position"]) for e in self.events)


class InMemoryCanonSource:
    """A canon graph + episodic store held as a JSON-able mapping."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        # {"entities": {id: entity}, "episodic": {shot_id: shot_record}}
        self.state: dict[str, Any] = state or {"entities": {}, "episodic": {}}

    async def dump(self) -> dict[str, Any]:
        # Deep-ish copy via canonical JSON round-trip so a caller cannot mutate.
        import json

        return json.loads(json.dumps(self.state, sort_keys=True))

    async def load(self, snapshot: dict[str, Any]) -> None:
        import json

        self.state = json.loads(json.dumps(snapshot, sort_keys=True))

    async def asset_keys(self) -> list[str]:
        keys: set[str] = set()
        for ent in self.state.get("entities", {}).values():
            for ref in ent.get("reference_images", []) or []:
                k = ref.get("key") if isinstance(ref, dict) else None
                if k:
                    keys.add(str(k))
            voice = ent.get("voice_key")
            if voice:
                keys.add(str(voice))
        for shot in self.state.get("episodic", {}).values():
            for field in ("clip_key", "audio_key", "last_frame_key"):
                k = shot.get(field)
                if k:
                    keys.add(str(k))
        return sorted(keys)


class InMemoryReadModelTarget:
    """A ``{namespace: {key: row}}`` read-model store with bulk dump/load."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    def put(self, namespace: str, key: str, row: dict[str, Any]) -> None:
        """Test helper: upsert a single row."""
        self.data.setdefault(namespace, {})[key] = dict(row)

    async def dump(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for ns, rows in self.data.items():
            out[ns] = [{"key": k, "value": v} for k, v in sorted(rows.items())]
        return out

    async def clear_all(self) -> None:
        self.data.clear()

    async def load(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.data.clear()
        for ns, items in rows.items():
            bucket = self.data.setdefault(ns, {})
            for item in items:
                bucket[str(item["key"])] = dict(item["value"])


class InMemoryAssetSource:
    """An object store fronting :class:`AssetSource` (key -> raw bytes)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        """Test helper: place an asset."""
        self._objects[key] = bytes(data)

    def remove(self, key: str) -> None:
        """Test helper: delete an asset (simulate loss/mismatch)."""
        self._objects.pop(key, None)

    def corrupt(self, key: str) -> None:
        """Test helper: mutate an asset's bytes (simulate silent corruption)."""
        if key in self._objects:
            self._objects[key] = self._objects[key] + b"\x00"

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def content_digest(self, key: str) -> str | None:
        raw = self._objects.get(key)
        if raw is None:
            return None
        return digest(list(raw))

    async def size(self, key: str) -> int:
        return len(self._objects.get(key, b""))

    async def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))


class InMemoryBackupRepository:
    """A dict-backed :class:`BackupRepository` (the snapshot vault)."""

    def __init__(self) -> None:
        self._store: dict[str, BackupManifest] = {}

    async def save(self, manifest: BackupManifest) -> None:
        self._store[manifest.descriptor.snapshot_id] = manifest

    async def get(self, snapshot_id: str) -> BackupManifest | None:
        return self._store.get(snapshot_id)

    async def list_ids(self) -> list[str]:
        return list(self._store)

    async def delete(self, snapshot_id: str) -> bool:
        return self._store.pop(snapshot_id, None) is not None

    async def all(self) -> Sequence[BackupManifest]:
        """Test helper: every stored manifest."""
        return list(self._store.values())


__all__ = [
    "AssetSource",
    "BackupRepository",
    "CanonSource",
    "EventSink",
    "EventSource",
    "InMemoryAssetSource",
    "InMemoryBackupRepository",
    "InMemoryCanonSource",
    "InMemoryEventSink",
    "InMemoryEventSource",
    "InMemoryReadModelTarget",
    "ReadModelTarget",
]
