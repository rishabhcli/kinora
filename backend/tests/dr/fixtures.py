"""Shared deterministic fixtures for the DR test-suite.

A small "world builder" that seeds the in-memory seams with a coherent story:
events on an event source, a canon graph + episodic shots referencing assets,
matching objects in the asset source, and a read-model derived from the events.
The :func:`example_projector` re-derives the read model purely from events so a
restore-by-projection lands at the same materialised state as the source — the
exact equivalence the chain/PITR tests assert.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.dr.interfaces import (
    InMemoryAssetSource,
    InMemoryBackupRepository,
    InMemoryCanonSource,
    InMemoryEventSink,
    InMemoryEventSource,
    InMemoryReadModelTarget,
    ReadModelTarget,
)


class World:
    """The injected seams + helpers for one deterministic test scenario."""

    def __init__(self) -> None:
        self.events = InMemoryEventSource()
        self.canon = InMemoryCanonSource()
        self.read_models = InMemoryReadModelTarget()
        self.assets = InMemoryAssetSource()
        self.repo = InMemoryBackupRepository()
        self.sink = InMemoryEventSink()

    def add_shot(
        self,
        shot_id: str,
        *,
        page: int,
        recorded_at: float | None = None,
        with_assets: bool = True,
    ) -> None:
        """Append a 'shot.accepted' event + the episodic record + its assets.

        Mirrors §8.2: a shot references a clip + audio in object storage. The read
        model (a 'shot board' counting accepted shots) is *derived from events* by
        :func:`example_projector`, never hand-maintained, so a rebuild is truthful.
        """
        clip = f"clips/book/{shot_id}.mp4"
        audio = f"audio/book/{shot_id}.wav"
        self.events.append(
            stream_id=f"shot:{shot_id}",
            type="shot.accepted",
            payload={"shot_id": shot_id, "page": page, "clip_key": clip, "audio_key": audio},
            recorded_at=recorded_at,
        )
        self.canon.state["episodic"][shot_id] = {
            "shot_id": shot_id,
            "page": page,
            "clip_key": clip,
            "audio_key": audio,
        }
        if with_assets:
            self.assets.put(clip, f"clip-bytes-{shot_id}".encode())
            self.assets.put(audio, f"audio-bytes-{shot_id}".encode())

    def add_entity(self, entity_id: str, name: str, *, ref_key: str) -> None:
        """Add a canon character with a locked reference image (§8.1)."""
        self.canon.state["entities"][entity_id] = {
            "id": entity_id,
            "name": name,
            "reference_images": [{"key": ref_key, "locked": True}],
        }
        self.assets.put(ref_key, f"ref-bytes-{entity_id}".encode())

    async def rebuild_read_model_from_events(self) -> None:
        """Apply the example projector to the *source* events (seed the RM)."""
        events = await self.events.read_range(0, await self.events.head_position())
        await example_projector(events, self.read_models)


async def example_projector(
    events: Sequence[dict[str, Any]],
    target: ReadModelTarget,
) -> None:
    """A truthful read-model fold: a per-shot board derived only from events.

    Idempotent + absolute (upsert by shot_id), so replaying the same events twice
    yields the same rows — the property a restore-by-projection relies on.
    """
    if isinstance(target, InMemoryReadModelTarget):
        target.data.clear()
    rows: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") == "shot.accepted":
            shot_id = ev["payload"]["shot_id"]
            rows[shot_id] = {
                "shot_id": shot_id,
                "page": ev["payload"]["page"],
                "status": "accepted",
                "position": ev["global_position"],
            }
    if isinstance(target, InMemoryReadModelTarget):
        for shot_id, row in rows.items():
            target.put("shot_board", shot_id, row)
    else:  # pragma: no cover - the fake is what tests use
        await target.load({"shot_board": [{"key": k, "value": v} for k, v in rows.items()]})


def fixed_clock(at: float = 1_000.0) -> datetime:
    """A deterministic UTC clock for accounting tests."""
    return datetime.fromtimestamp(at, tz=UTC)


__all__ = ["World", "example_projector", "fixed_clock"]
