"""Unit tests for the in-memory alias registry + version allocation."""

from __future__ import annotations

import time

from app.search.alias import DEFAULT_ALIAS, InMemoryAliasRegistry, new_version


async def test_resolve_unset_is_none() -> None:
    reg = InMemoryAliasRegistry()
    assert await reg.resolve("missing") is None


async def test_set_and_resolve() -> None:
    reg = InMemoryAliasRegistry()
    await reg.set_alias(DEFAULT_ALIAS, "v1")
    assert await reg.resolve(DEFAULT_ALIAS) == "v1"


async def test_swap_is_atomic_replace() -> None:
    reg = InMemoryAliasRegistry({DEFAULT_ALIAS: "v1"})
    await reg.set_alias(DEFAULT_ALIAS, "v2")
    assert await reg.resolve(DEFAULT_ALIAS) == "v2"


def test_new_version_is_monotonic() -> None:
    a = new_version()
    time.sleep(0.002)
    b = new_version()
    assert a < b
    assert a.startswith("v")
