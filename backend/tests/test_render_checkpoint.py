"""Checkpoint / restore of in-flight shot renders (kinora.md §9.7, §4.11).

Snapshot serialisation, the in-memory + JSON stores, stale-write protection, and
the resume decision (fresh / resume / skip-terminal). No ffmpeg/DB/network.
"""

from __future__ import annotations

from typing import Any

from app.render.checkpoint import (
    InMemoryCheckpointStore,
    JsonCheckpointStore,
    ShotCheckpoint,
    probe_resume,
)
from app.render.ladder import Rung
from app.render.states import RenderState
from app.render.steps import Step, StepLedger


def _checkpoint(**over: Any) -> ShotCheckpoint:
    ledger = StepLedger(shot_id="shot_42")
    ledger.record(Step.RESERVE, "k1", "res_1")
    base: dict[str, Any] = {
        "shot_id": "shot_42",
        "book_id": "book_demo",
        "state": RenderState.RENDERING,
        "attempts": 1,
        "spent_video_seconds": 5.0,
        "spec_digest": "digest_a",
        "last_rung": Rung.FULL_WAN,
        "reason": "provider_error",
        "ledger": ledger,
    }
    base.update(over)
    return ShotCheckpoint(**base)


def test_snapshot_serialisation_roundtrips() -> None:
    cp = _checkpoint()
    restored = ShotCheckpoint.from_dict(cp.as_dict())
    assert restored.shot_id == "shot_42"
    assert restored.state is RenderState.RENDERING
    assert restored.attempts == 1
    assert restored.spent_video_seconds == 5.0
    assert restored.last_rung is Rung.FULL_WAN
    assert restored.ledger is not None
    assert restored.ledger.is_done(Step.RESERVE, "k1")


def test_terminal_flag() -> None:
    assert _checkpoint(state=RenderState.ACCEPTED).is_terminal
    assert _checkpoint(state=RenderState.DEGRADED).is_terminal
    assert not _checkpoint(state=RenderState.RENDERING).is_terminal


def test_bump_increments_revision_without_mutating() -> None:
    cp = _checkpoint(revision=2)
    bumped = cp.bump()
    assert bumped.revision == 3
    assert cp.revision == 2


async def test_in_memory_store_save_load_clear() -> None:
    store = InMemoryCheckpointStore()
    assert await store.load("shot_42") is None
    await store.save(_checkpoint())
    loaded = await store.load("shot_42")
    assert loaded is not None and loaded.attempts == 1
    await store.clear("shot_42")
    assert await store.load("shot_42") is None
    assert store.saves == 1 and store.clears == 1


async def test_in_memory_store_rejects_stale_write() -> None:
    store = InMemoryCheckpointStore()
    await store.save(_checkpoint(revision=5))
    # An older-revision write must not clobber the newer snapshot.
    await store.save(_checkpoint(revision=3, attempts=99))
    loaded = await store.load("shot_42")
    assert loaded is not None and loaded.revision == 5 and loaded.attempts == 1


async def test_probe_resume_fresh_when_no_checkpoint() -> None:
    decision = await probe_resume(InMemoryCheckpointStore(), "shot_42")
    assert decision.skip is False
    assert decision.checkpoint is None
    assert decision.reason == "fresh"


async def test_probe_resume_resumes_mid_flight() -> None:
    store = InMemoryCheckpointStore()
    await store.save(_checkpoint(state=RenderState.RENDERING))
    decision = await probe_resume(store, "shot_42")
    assert decision.skip is False
    assert decision.checkpoint is not None
    assert decision.reason == "resume"


async def test_probe_resume_skips_terminal_checkpoint() -> None:
    store = InMemoryCheckpointStore()
    await store.save(_checkpoint(state=RenderState.ACCEPTED))
    decision = await probe_resume(store, "shot_42")
    assert decision.skip is True
    assert decision.reason == "already_terminal"


async def test_json_store_codec_over_dict_backend() -> None:
    class DictBackend:
        def __init__(self) -> None:
            self.kv: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.kv.get(key)

        async def set(self, key: str, value: str) -> None:
            self.kv[key] = value

        async def delete(self, key: str) -> None:
            self.kv.pop(key, None)

    backend = DictBackend()
    store = JsonCheckpointStore(backend=backend)
    await store.save(_checkpoint())
    # Stored as a real JSON string under the prefixed key.
    assert backend.kv["render:checkpoint:shot_42"].startswith("{")
    loaded = await store.load("shot_42")
    assert loaded is not None and loaded.spec_digest == "digest_a"
    assert loaded.ledger is not None and loaded.ledger.result_of(Step.RESERVE) == "res_1"
    await store.clear("shot_42")
    assert await store.load("shot_42") is None
