"""Edge-cache policy: TTL, immutable content-addressed assets, purge-on-invalidate."""

from __future__ import annotations

import pytest

from app.cdn.cache import (
    DEFAULT_MUTABLE_TTL_S,
    IMMUTABLE_TTL_S,
    CacheClass,
    EdgeCachePolicy,
    classify_key,
    invalidation_keys_for_shot,
)
from app.cdn.errors import CachePurgeError
from app.cdn.testing import FakeCdnProvider
from app.media.hashing import CONTENT_ADDRESS_PREFIX

IMMUTABLE_KEY = f"{CONTENT_ADDRESS_PREFIX}/ab/cd/abcd1234.mp4"
MUTABLE_KEY = "clips/book1/shot_00001.mp4"


def test_classify_content_addressed_is_immutable() -> None:
    assert classify_key(IMMUTABLE_KEY) is CacheClass.IMMUTABLE
    assert classify_key("/" + IMMUTABLE_KEY) is CacheClass.IMMUTABLE  # leading slash ok
    assert classify_key(MUTABLE_KEY) is CacheClass.MUTABLE


def test_immutable_policy_long_ttl_no_purge() -> None:
    policy = EdgeCachePolicy().policy_for(IMMUTABLE_KEY)
    assert policy.immutable is True
    assert policy.ttl_s == IMMUTABLE_TTL_S
    assert policy.purge_on_invalidate is False
    assert "immutable" in policy.cache_control_header()
    assert f"max-age={IMMUTABLE_TTL_S}" in policy.cache_control_header()


def test_mutable_policy_bounded_ttl_and_purge() -> None:
    policy = EdgeCachePolicy().policy_for(MUTABLE_KEY)
    assert policy.immutable is False
    assert policy.ttl_s == DEFAULT_MUTABLE_TTL_S
    assert policy.purge_on_invalidate is True
    header = policy.cache_control_header()
    assert "stale-while-revalidate" in header
    assert "immutable" not in header


def test_custom_ttls_respected() -> None:
    policy = EdgeCachePolicy(immutable_ttl_s=10, mutable_ttl_s=5)
    assert policy.policy_for(IMMUTABLE_KEY).ttl_s == 10
    assert policy.policy_for(MUTABLE_KEY).ttl_s == 5


async def test_invalidate_purges_mutable_across_providers() -> None:
    providers = [FakeCdnProvider("eu"), FakeCdnProvider("ap")]
    for p in providers:
        await p.warm(MUTABLE_KEY, "http://origin/x")
    policy = await EdgeCachePolicy().invalidate(MUTABLE_KEY, providers)
    assert policy.purge_on_invalidate is True
    for p in providers:
        assert MUTABLE_KEY in p.purged
        assert not await p.is_cached(MUTABLE_KEY)


async def test_invalidate_immutable_is_noop() -> None:
    providers = [FakeCdnProvider("eu")]
    await providers[0].warm(IMMUTABLE_KEY, "http://origin/x")
    await EdgeCachePolicy().invalidate(IMMUTABLE_KEY, providers)
    # Immutable bytes never change, so a stale edge copy is still correct: no purge.
    assert providers[0].purged == []
    assert await providers[0].is_cached(IMMUTABLE_KEY)


async def test_invalidate_attempts_all_then_raises_on_failure() -> None:
    ok = FakeCdnProvider("eu")
    bad = FakeCdnProvider("ap", fail_invalidate=True)
    await ok.warm(MUTABLE_KEY, "http://origin/x")
    with pytest.raises(CachePurgeError):
        await EdgeCachePolicy().invalidate(MUTABLE_KEY, [ok, bad])
    # The healthy edge was still purged despite the other failing.
    assert MUTABLE_KEY in ok.purged


def test_invalidation_keys_for_shot_targets_clip_path() -> None:
    keys = invalidation_keys_for_shot("book1", "shot_00001")
    assert keys == ("clips/book1/shot_00001.mp4",)
