"""Shared deterministic fixtures for orchestration tests (zero infra, virtual clock).

Every test runs against an :class:`InMemoryOrchestrationStore` and a
:class:`VirtualClock` — no Redis, no network, no real time. ``KINORA_LIVE_VIDEO``
is never enabled here; the orchestrator decides placement and never renders.
"""

from __future__ import annotations

import pytest

from app.orchestration.clock import VirtualClock
from app.orchestration.models import (
    Lane,
    ShotTicket,
    WorkerCapabilities,
)
from app.orchestration.store import InMemoryOrchestrationStore


@pytest.fixture
def clock() -> VirtualClock:
    """A virtual clock starting at t=0; tests advance it explicitly."""
    return VirtualClock(start_ms=0)


@pytest.fixture
def store(clock: VirtualClock) -> InMemoryOrchestrationStore:
    return InMemoryOrchestrationStore(clock)


def caps(
    *lanes: Lane,
    providers: tuple[str, ...] = (),
    max_concurrency: int = 1,
) -> WorkerCapabilities:
    """Concise capability builder for tests."""
    return WorkerCapabilities(
        lanes=frozenset(lanes),
        providers=frozenset(providers),
        max_concurrency=max_concurrency,
    )


def ticket(
    shot_hash: str,
    *,
    book_id: str = "book-1",
    lane: Lane = Lane.COMMITTED,
    provider: str = "wan",
    video_seconds: float = 5.0,
) -> ShotTicket:
    """Concise ticket builder for tests."""
    return ShotTicket(
        shot_hash=shot_hash,
        book_id=book_id,
        lane=lane,
        provider=provider,
        video_seconds=video_seconds,
    )
