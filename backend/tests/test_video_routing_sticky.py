"""Unit tests for sticky routing: family-key derivation, the bounded-LRU store, and
the apply_stickiness promotion (a preference, never a resurrection)."""

from __future__ import annotations

from app.providers.types import WanMode, WanSpec
from app.video.routing.sticky import StickyStore, apply_stickiness, family_key


def test_family_key_strips_last_segment() -> None:
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="sess1:seg3:shot7")
    assert family_key(spec) == "sess1:seg3"


def test_family_key_flat_id_is_itself() -> None:
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="shot7")
    assert family_key(spec) == "shot7"


def test_family_key_none_without_shot_id() -> None:
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO)
    assert family_key(spec) is None


def test_sticky_store_get_set_roundtrip() -> None:
    store = StickyStore(capacity=4)
    assert store.get("fam") is None
    store.set("fam", "video:turbo")
    assert store.get("fam") == "video:turbo"


def test_sticky_store_ignores_none_key() -> None:
    store = StickyStore()
    store.set(None, "x")
    assert store.get(None) is None
    assert len(store) == 0


def test_sticky_store_lru_eviction() -> None:
    store = StickyStore(capacity=2)
    store.set("a", "1")
    store.set("b", "2")
    # touch "a" so "b" is the LRU.
    assert store.get("a") == "1"
    store.set("c", "3")  # evicts "b"
    assert store.get("b") is None
    assert store.get("a") == "1"
    assert store.get("c") == "3"


def test_apply_stickiness_promotes_pinned() -> None:
    ranked = ["a", "b", "c"]
    assert apply_stickiness(ranked, "c") == ["c", "a", "b"]


def test_apply_stickiness_noop_when_pinned_missing() -> None:
    # Pinned backend is not routable (filtered out / breaker open) -> ignored.
    ranked = ["a", "b"]
    assert apply_stickiness(ranked, "z") == ["a", "b"]
    assert apply_stickiness(ranked, None) == ["a", "b"]
