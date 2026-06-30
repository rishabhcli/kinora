"""Unit tests for the ProviderRegistry: registration guards, capability queries,
and deterministic selection strategies (priority / cheapest / best-quality /
shortest-floor) with ties resolving to registration order.
"""

from __future__ import annotations

import pytest

from app.video.abstraction.capability import (
    CapabilityQuery,
    SubmitStyle,
    VideoCapability,
    VideoMode,
)
from app.video.abstraction.echo import EchoVideoProvider, default_echo_capability
from app.video.abstraction.registry import (
    DuplicateProvider,
    ProviderNotFound,
    ProviderRanking,
    ProviderRegistry,
    SelectionStrategy,
)


def _provider(pid: str, cap: VideoCapability | None = None) -> EchoVideoProvider:
    return EchoVideoProvider(cap or default_echo_capability(pid))


def _cap(pid: str, **kw: object) -> VideoCapability:
    base: dict[str, object] = {
        "provider_id": pid,
        "modes": frozenset(VideoMode),
        "min_duration_s": 1.0,
        "max_duration_s": 10.0,
        "resolutions": ("480P", "720P", "1080P"),
    }
    base.update(kw)
    return VideoCapability(**base)  # type: ignore[arg-type]


# -- registration --------------------------------------------------------- #


def test_register_and_get() -> None:
    reg = ProviderRegistry()
    p = _provider("a")
    reg.register(p)
    assert reg.get("a") is p
    assert "a" in reg
    assert len(reg) == 1
    assert reg.ids() == ("a",)


def test_register_duplicate_rejected_unless_overwrite() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("a"))
    with pytest.raises(DuplicateProvider):
        reg.register(_provider("a"))
    new = _provider("a")
    reg.register(new, overwrite=True)
    assert reg.get("a") is new
    assert reg.ids() == ("a",)  # overwrite keeps a single slot, same position


def test_register_rejects_id_capability_mismatch() -> None:
    reg = ProviderRegistry()
    p = EchoVideoProvider(default_echo_capability("declared"))
    p.provider_id = "mismatch"  # diverge the attribute from the capability id
    with pytest.raises(ValueError, match="!= capabilities"):
        reg.register(p)


def test_get_missing_raises() -> None:
    with pytest.raises(ProviderNotFound):
        ProviderRegistry().get("nope")


def test_unregister() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("a"))
    reg.register(_provider("b"))
    reg.unregister("a")
    assert "a" not in reg
    assert reg.ids() == ("b",)
    reg.unregister("ghost")  # no-op


def test_capabilities_snapshot() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("a"))
    caps = reg.capabilities()
    assert set(caps) == {"a"}
    assert caps["a"].provider_id == "a"


# -- find / query --------------------------------------------------------- #


def test_find_by_mode_and_resolution() -> None:
    reg = ProviderRegistry()
    t2v_cap = _cap("t2v-only", modes=frozenset({VideoMode.TEXT_TO_VIDEO}))
    reg.register(_provider("t2v-only", t2v_cap))
    reg.register(_provider("full", _cap("full")))
    found = reg.find(CapabilityQuery(mode=VideoMode.REFERENCE_TO_VIDEO, resolution="1080P"))
    assert [p.provider_id for p in found] == ["full"]


def test_find_returns_priority_order() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("first"))
    reg.register(_provider("second"))
    found = reg.find(CapabilityQuery())
    assert [p.provider_id for p in found] == ["first", "second"]


def test_find_async_constraint() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("sync", _cap("sync", submit_style=SubmitStyle.SYNCHRONOUS)))
    reg.register(_provider("async", _cap("async", submit_style=SubmitStyle.ASYNC_POLL)))
    assert [p.provider_id for p in reg.find(CapabilityQuery(needs_async=True))] == ["async"]
    assert [p.provider_id for p in reg.find(CapabilityQuery(needs_async=False))] == ["sync"]


# -- select --------------------------------------------------------------- #


def test_select_priority_default() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("first"))
    reg.register(_provider("second"))
    chosen = reg.select(CapabilityQuery(mode=VideoMode.TEXT_TO_VIDEO))
    assert chosen.provider_id == "first"


def test_select_no_match_raises() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("t2v", _cap("t2v", modes=frozenset({VideoMode.TEXT_TO_VIDEO}))))
    with pytest.raises(ProviderNotFound):
        reg.select(CapabilityQuery(mode=VideoMode.INSTRUCTION_EDIT))


def test_select_cheapest_prefers_low_cost() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("quality"), ranking=ProviderRanking(cost_per_s=4.0, quality=0.9))
    reg.register(_provider("turbo"), ranking=ProviderRanking(cost_per_s=1.0, quality=0.5))
    chosen = reg.select(CapabilityQuery(), strategy=SelectionStrategy.CHEAPEST)
    assert chosen.provider_id == "turbo"


def test_select_best_quality_prefers_high_quality() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("turbo"), ranking=ProviderRanking(cost_per_s=1.0, quality=0.5))
    reg.register(_provider("quality"), ranking=ProviderRanking(cost_per_s=4.0, quality=0.9))
    chosen = reg.select(CapabilityQuery(), strategy=SelectionStrategy.BEST_QUALITY)
    assert chosen.provider_id == "quality"


def test_select_shortest_floor() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("big", _cap("big", min_duration_s=5.0)))
    reg.register(_provider("tiny", _cap("tiny", min_duration_s=1.0)))
    chosen = reg.select(CapabilityQuery(), strategy=SelectionStrategy.SHORTEST_FLOOR)
    assert chosen.provider_id == "tiny"


def test_select_ties_resolve_to_priority_order() -> None:
    reg = ProviderRegistry()
    # equal cost → registration order decides
    reg.register(_provider("a"), ranking=ProviderRanking(cost_per_s=2.0))
    reg.register(_provider("b"), ranking=ProviderRanking(cost_per_s=2.0))
    chosen = reg.select(CapabilityQuery(), strategy=SelectionStrategy.CHEAPEST)
    assert chosen.provider_id == "a"


def test_rank_neutral_for_unranked_provider() -> None:
    reg = ProviderRegistry()
    reg.register(_provider("ranked"), ranking=ProviderRanking(cost_per_s=0.1))
    reg.register(_provider("unranked"))  # neutral cost_per_s == 1.0
    chosen = reg.select(CapabilityQuery(), strategy=SelectionStrategy.CHEAPEST)
    assert chosen.provider_id == "ranked"
