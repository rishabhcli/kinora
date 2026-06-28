"""Unit tests for the transcode queue seam + retention policy (pure)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.media.kinds import MediaAssetKind
from app.media.lifecycle import RetentionPolicy
from app.media.transcode import (
    DEFAULT_DERIVATIONS,
    Derivation,
    InMemoryTranscodeQueue,
    TranscodeJob,
)

# -- transcode queue --------------------------------------------------------- #


@pytest.mark.asyncio
async def test_queue_fifo_and_depth() -> None:
    q = InMemoryTranscodeQueue()
    assert await q.depth() == 0
    j1 = TranscodeJob(source_key="a.mp4")
    j2 = TranscodeJob(source_key="b.mp4")
    await q.enqueue(j1)
    await q.enqueue(j2)
    assert await q.depth() == 2
    first = await q.dequeue()
    assert first is not None and first.source_key == "a.mp4"
    second = await q.dequeue()
    assert second is not None and second.source_key == "b.mp4"
    assert await q.dequeue() is None


@pytest.mark.asyncio
async def test_queue_dedups_by_job_id() -> None:
    q = InMemoryTranscodeQueue()
    job = TranscodeJob(source_key="x.mp4").with_id("fixed-id")
    await q.enqueue(job)
    await q.enqueue(job)  # same id → no double
    assert await q.depth() == 1


def test_transcode_job_defaults() -> None:
    job = TranscodeJob(source_key="s.mp4")
    assert job.derivations == DEFAULT_DERIVATIONS
    assert Derivation.POSTER in job.derivations
    assert job.job_id  # auto id


def test_transcode_job_with_id_is_copy() -> None:
    job = TranscodeJob(source_key="s.mp4", book_id="bk")
    copy = job.with_id("k")
    assert copy.job_id == "k"
    assert copy.source_key == "s.mp4"
    assert copy.book_id == "bk"
    assert job.job_id != "k"


# -- retention policy -------------------------------------------------------- #


def test_retention_derived_gets_horizon() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = RetentionPolicy(derived_retention_days=30)
    exp = policy.expires_at(MediaAssetKind.POSTER, now=now)
    assert exp == now + timedelta(days=30)


def test_retention_primary_never_expires() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = RetentionPolicy(derived_retention_days=30)
    assert policy.expires_at(MediaAssetKind.CLIP, now=now) is None
    assert policy.expires_at(MediaAssetKind.SOURCE, now=now) is None
    assert policy.expires_at(MediaAssetKind.AUDIO, now=now) is None


def test_retention_disabled() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = RetentionPolicy(derived_retention_days=0)
    assert policy.is_enabled is False
    assert policy.expires_at(MediaAssetKind.SPRITE, now=now) is None


def test_retention_all_derived_kinds_get_horizon() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = RetentionPolicy(derived_retention_days=7)
    for kind in (
        MediaAssetKind.POSTER,
        MediaAssetKind.THUMBNAIL,
        MediaAssetKind.SPRITE,
        MediaAssetKind.VTT,
        MediaAssetKind.HLS,
        MediaAssetKind.DASH,
    ):
        assert policy.expires_at(kind, now=now) == now + timedelta(days=7)
