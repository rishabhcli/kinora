"""Shared, network-free helpers for the agent unit tests (no tests of its own).

The agents are exercised with REAL ``Providers`` whose high-level methods
(``chat.chat_json`` / ``vl.analyze_json`` / ``embeddings.embed_images`` /
``chat.chat`` / ``video.render``) are replaced with canned async stand-ins, so
no DashScope call ever happens. This mirrors how the providers are monkeypatched
throughout the suite.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from app.core.config import Settings
from app.providers import Providers, create_providers

_DIM = 1152


def make_providers(*, live_video: bool = False) -> Providers:
    """A real provider aggregate with an explicit (off-by-default) video gate."""
    settings = Settings(dashscope_api_key="test", kinora_live_video=live_video)
    return create_providers(settings)


@pytest_asyncio.fixture
async def providers() -> AsyncIterator[Providers]:
    """A provider aggregate, closed on teardown (no network is ever made)."""
    aggregate = make_providers()
    try:
        yield aggregate
    finally:
        await aggregate.aclose()


class JsonSequencer:
    """Async stand-in: returns each queued value once, then repeats the last.

    Works for ``chat_json`` / ``analyze_json`` (returns dicts) and for ``chat``
    (returns ``ChatResult`` objects). Ignores call args; counts calls so a test
    can assert the repair round-trip fired.
    """

    def __init__(self, *values: Any) -> None:
        self.values = list(values)
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


def one_hot(data: bytes) -> list[float]:
    """A deterministic 1152-d one-hot vector keyed by content (cosine 1.0 vs self)."""
    axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % _DIM
    vector = [0.0] * _DIM
    vector[axis] = 1.0
    return vector


class OneHotEmbedder:
    """Async stand-in for ``embeddings.embed_images``: one-hot per input image."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, images: list[bytes]) -> list[list[float]]:
        self.calls += 1
        return [one_hot(b) for b in images]


class FakeSkills:
    """Minimal ``QwenSkillDispatcher`` stand-in for the tool-loop test."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._result = result or {"ok": True}

    async def dispatch(self, name: str, arguments: Any) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._result
