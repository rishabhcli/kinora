"""Foundation tests: clock, protocol value objects, metrics, tokenize, fakes.

Pure and deterministic — no infra, no network, no sleeping.
"""

from __future__ import annotations

import pytest

from app.inference.accel.clock import FakeClock, SystemClock
from app.inference.accel.fakes import (
    HashEmbedder,
    ScriptedDraft,
    ScriptedTarget,
)
from app.inference.accel.metrics import (
    CacheMetrics,
    FanOutMetrics,
    PrefixReuseMetrics,
    SpeculativeMetrics,
)
from app.inference.accel.protocol import (
    GenerationRequest,
    GenerationResult,
)
from app.inference.accel.tokenize import (
    common_prefix,
    common_prefix_length,
    word_tokens,
)

# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


def test_fake_clock_lockstep_and_monotonic() -> None:
    clk = FakeClock(start=1000.0)
    assert clk.time() == 1000.0
    assert clk.monotonic() == 0.0
    clk.advance(2.5)
    assert clk.time() == 1002.5
    assert clk.monotonic() == 2.5
    with pytest.raises(ValueError):
        clk.advance(-1.0)


def test_fake_clock_wall_jump_does_not_touch_monotonic() -> None:
    clk = FakeClock(start=1000.0)
    clk.advance(5.0)
    clk.set_wall(9999.0)
    assert clk.time() == 9999.0
    assert clk.monotonic() == 5.0  # unaffected
    with pytest.raises(ValueError):
        clk.set_wall(0.0)


def test_system_clock_is_monotonic_nondecreasing() -> None:
    clk = SystemClock()
    a = clk.monotonic()
    b = clk.monotonic()
    assert b >= a
    assert clk.time() > 0


# --------------------------------------------------------------------------- #
# Protocol value objects
# --------------------------------------------------------------------------- #


def test_request_is_frozen_hashable_and_stable() -> None:
    r1 = GenerationRequest.from_prompt("hello world", model="m")
    r2 = GenerationRequest.from_prompt("hello world", model="m")
    assert r1 == r2
    assert hash(r1) == hash(r2)
    # Used as a dict key (cache key material).
    assert {r1: 1}[r2] == 1


def test_request_prompt_text_concatenates_messages() -> None:
    r = GenerationRequest.from_messages(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}]
    )
    assert r.prompt_text == "be terse\nhi"


def test_request_tags_are_sorted_and_frozen() -> None:
    r = GenerationRequest.from_prompt("x", tags={"b": "2", "a": "1"})
    assert r.tags == (("a", "1"), ("b", "2"))


def test_request_builders() -> None:
    r = GenerationRequest.from_prompt("x").with_max_tokens(99).with_model("y")
    assert r.max_tokens == 99
    assert r.model == "y"


def test_result_from_tokens_and_with_meta() -> None:
    res = GenerationResult.from_tokens(["a", "b", "c"], model="t")
    assert res.text == "a b c"
    assert res.output_tokens == 3
    assert res.model == "t"
    enriched = res.with_meta(source="cache")
    assert enriched.meta["source"] == "cache"
    assert "source" not in res.meta  # original untouched


# --------------------------------------------------------------------------- #
# Tokenize
# --------------------------------------------------------------------------- #


def test_word_tokens_and_common_prefix() -> None:
    assert word_tokens("the quick   brown fox") == ("the", "quick", "brown", "fox")
    a = ("the", "quick", "brown", "fox")
    b = ("the", "quick", "red", "fox")
    assert common_prefix_length(a, b) == 2
    assert common_prefix(a, b) == ("the", "quick")
    assert common_prefix_length((), a) == 0
    assert common_prefix_length(a, a) == len(a)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_speculative_metrics_acceptance_rate() -> None:
    m = SpeculativeMetrics()
    m.record_round(proposed=4, accepted=3, bonus=1)
    m.record_round(proposed=4, accepted=1, bonus=1)
    snap = m.snapshot()
    assert snap.rounds == 2
    assert snap.proposed_tokens == 8
    assert snap.accepted_tokens == 4
    assert snap.acceptance_rate == 0.5
    assert snap.committed_tokens == 6  # 4 accepted + 2 bonus
    assert snap.target_calls == 2
    assert snap.tokens_per_target_call == 3.0


def test_cache_metrics_hit_rate() -> None:
    m = CacheMetrics()
    m.record_lookup(exact_hit=True)
    m.record_lookup(semantic_hit=True)
    m.record_lookup(near_miss=True)  # a miss that was a near-miss
    m.record_store()
    snap = m.snapshot()
    assert snap.lookups == 3
    assert snap.exact_hits == 1
    assert snap.semantic_hits == 1
    assert snap.misses == 1
    assert snap.hits == 2
    assert snap.hit_rate == pytest.approx(2 / 3)
    assert snap.near_miss_rejects == 1
    assert snap.stores == 1


def test_fanout_metrics() -> None:
    m = FanOutMetrics()
    m.record_race(started=3, cancelled=2, won=True, failures=0, cost=1.5)
    m.record_cap_rejection()
    snap = m.snapshot()
    assert snap.races == 1
    assert snap.candidates_started == 3
    assert snap.candidates_cancelled == 2
    assert snap.wins == 1
    assert snap.cost_charged == 1.5
    assert snap.cap_rejections == 1
    assert snap.mean_candidates_per_race == 3.0


def test_prefix_reuse_metrics() -> None:
    m = PrefixReuseMetrics()
    m.record_request(prompt_tokens=100, reused_tokens=80, blocks_reused=4, blocks_allocated=1)
    snap = m.snapshot()
    assert snap.reuse_rate == 0.8
    assert snap.blocks_reused == 4
    assert snap.blocks_allocated == 1


def test_metrics_zero_division_safe() -> None:
    assert SpeculativeMetrics().snapshot().acceptance_rate == 0.0
    assert CacheMetrics().snapshot().hit_rate == 0.0
    assert FanOutMetrics().snapshot().mean_candidates_per_race == 0.0
    assert PrefixReuseMetrics().snapshot().reuse_rate == 0.0


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


async def test_scripted_target_generate_and_verify_agree() -> None:
    target = ScriptedTarget({"prompt": ["a", "b", "c", "d"]}, key_fn=lambda r: "prompt")
    req = GenerationRequest.from_prompt("anything", max_tokens=10)
    gen = await target.generate(req)
    assert gen.tokens == ("a", "b", "c", "d")
    # verify from empty committed, proposing 2 tokens -> next tokens at 3 positions
    nexts = await target.verify(req, (), ("a", "b"))
    assert nexts == ("a", "b", "c")
    # past the end yields ""
    nexts2 = await target.verify(req, ("a", "b", "c", "d"), ("x",))
    assert nexts2 == ("", "")


async def test_scripted_target_respects_max_tokens() -> None:
    target = ScriptedTarget(default_fn=lambda r: ["a", "b", "c", "d", "e"])
    req = GenerationRequest.from_prompt("x", max_tokens=2)
    gen = await target.generate(req)
    assert gen.tokens == ("a", "b")


async def test_scripted_draft_corruption_and_stop() -> None:
    oracle = lambda r: ["a", "b", "c", "d", "e"]  # noqa: E731
    draft = ScriptedDraft(oracle=oracle, corrupt_at=2)
    req = GenerationRequest.from_prompt("x")
    prop = await draft.propose(req, (), 4)
    assert prop.tokens == ("a", "b", "__WRONG__c", "d")
    assert len(prop.confidences) == 4

    capped = ScriptedDraft(oracle=oracle, stop_after=2)
    prop2 = await capped.propose(req, (), 4)
    assert prop2.tokens == ("a", "b")


async def test_hash_embedder_deterministic_unit_and_alias() -> None:
    emb = HashEmbedder(dim=8)
    v1 = await emb.embed("hello")
    v2 = await emb.embed("hello")
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert norm == pytest.approx(1.0)

    aliased = HashEmbedder(dim=8, alias={"hi there": "hello"})
    assert await aliased.embed("hi there") == await aliased.embed("hello")
