"""Tests for app.inference.router.binpack — token-budget bin-packing.

Pins the in-flight-batching contract: token + slot + prefill-chunk budgets are
all honoured, input order is preserved, oversized requests are surfaced (never
silently dropped), and a smaller later request can ride along when a bigger
earlier one didn't fit (bounded look-ahead).
"""

from __future__ import annotations

import pytest

from app.inference.router.binpack import BatchBudget, TokenBinPacker, total_tokens
from app.inference.router.errors import RouterConfigError
from app.inference.router.request import InferenceRequest


def _req(rid: str, *, prompt: int = 0, out: int = 0) -> InferenceRequest:
    return InferenceRequest(request_id=rid, model="m", prompt_tokens=prompt, max_output_tokens=out)


def test_packs_until_token_budget_exhausted() -> None:
    packer = TokenBinPacker()
    reqs = [_req(f"r{i}", prompt=100) for i in range(10)]
    res = packer.pack(reqs, BatchBudget(token_budget=350, slot_budget=100))
    assert [r.request_id for r in res.batch] == ["r0", "r1", "r2"]
    assert res.tokens_used == 300
    assert len(res.deferred) == 7


def test_slot_budget_caps_batch_count() -> None:
    packer = TokenBinPacker()
    reqs = [_req(f"r{i}", prompt=1) for i in range(10)]
    res = packer.pack(reqs, BatchBudget(token_budget=10_000, slot_budget=3))
    assert len(res.batch) == 3
    assert len(res.deferred) == 7


def test_preserves_input_order() -> None:
    packer = TokenBinPacker()
    reqs = [_req("a", prompt=10), _req("b", prompt=10), _req("c", prompt=10)]
    res = packer.pack(reqs, BatchBudget(token_budget=100, slot_budget=100))
    assert [r.request_id for r in res.batch] == ["a", "b", "c"]


def test_smaller_later_request_rides_along() -> None:
    # 'big' doesn't fit the remaining budget but 'small' does — first-fit skips.
    packer = TokenBinPacker()
    reqs = [_req("first", prompt=60), _req("big", prompt=60), _req("small", prompt=20)]
    res = packer.pack(reqs, BatchBudget(token_budget=80, slot_budget=100))
    ids = [r.request_id for r in res.batch]
    assert "first" in ids and "small" in ids
    assert "big" in [r.request_id for r in res.deferred]


def test_oversized_request_is_surfaced_not_dropped() -> None:
    packer = TokenBinPacker()
    reqs = [_req("huge", prompt=1000), _req("ok", prompt=10)]
    res = packer.pack(reqs, BatchBudget(token_budget=100, slot_budget=100))
    assert [r.request_id for r in res.oversized] == ["huge"]
    assert [r.request_id for r in res.batch] == ["ok"]


def test_prefill_chunk_budget_limits_new_prompt_tokens() -> None:
    packer = TokenBinPacker()
    reqs = [_req("p1", prompt=80), _req("p2", prompt=80)]
    res = packer.pack(
        reqs, BatchBudget(token_budget=10_000, slot_budget=100, prefill_chunk_budget=100)
    )
    # Only the first fits the 100-token prefill chunk; the second defers.
    assert [r.request_id for r in res.batch] == ["p1"]
    assert [r.request_id for r in res.deferred] == ["p2"]
    assert res.prefill_used == 80


def test_request_too_big_for_prefill_chunk_is_oversized() -> None:
    packer = TokenBinPacker()
    res = packer.pack(
        [_req("p", prompt=200)],
        BatchBudget(token_budget=10_000, slot_budget=100, prefill_chunk_budget=100),
    )
    assert [r.request_id for r in res.oversized] == ["p"]


def test_lookahead_bounds_queue_hopping() -> None:
    # 'first' (70) fits the empty 100-budget, leaving 30. The two mediums (40
    # each) fit the *whole* budget but not the 30 remaining, so they are
    # deferred (not oversized); with lookahead=1, after skipping >1 of them we
    # stop hopping before reaching 'small', preserving near-FIFO order.
    packer = TokenBinPacker(lookahead=1)
    reqs = [
        _req("first", prompt=70),
        _req("med1", prompt=40),
        _req("med2", prompt=40),
        _req("small", prompt=10),
    ]
    res = packer.pack(reqs, BatchBudget(token_budget=100, slot_budget=100))
    assert [r.request_id for r in res.batch] == ["first"]
    # med1, med2 skipped; lookahead (1) exhausted -> small never reached.
    assert {r.request_id for r in res.deferred} == {"med1", "med2", "small"}
    assert not res.oversized


def test_unbounded_lookahead_lets_small_ride_along() -> None:
    # Same shape, but no lookahead cap: 'small' still fits the 30 remaining.
    packer = TokenBinPacker(lookahead=None)
    reqs = [
        _req("first", prompt=70),
        _req("med1", prompt=40),
        _req("med2", prompt=40),
        _req("small", prompt=10),
    ]
    res = packer.pack(reqs, BatchBudget(token_budget=100, slot_budget=100))
    assert {r.request_id for r in res.batch} == {"first", "small"}
    assert {r.request_id for r in res.deferred} == {"med1", "med2"}


def test_zero_slot_budget_makes_everything_oversized() -> None:
    packer = TokenBinPacker()
    res = packer.pack([_req("a", prompt=1)], BatchBudget(token_budget=100, slot_budget=0))
    assert [r.request_id for r in res.oversized] == ["a"]


def test_empty_input() -> None:
    packer = TokenBinPacker()
    res = packer.pack([], BatchBudget(token_budget=100, slot_budget=10))
    assert res.is_empty
    assert not res.deferred and not res.oversized


def test_negative_budget_rejected() -> None:
    with pytest.raises(RouterConfigError):
        BatchBudget(token_budget=-1, slot_budget=1)
    with pytest.raises(RouterConfigError):
        BatchBudget(token_budget=1, slot_budget=-1)
    with pytest.raises(RouterConfigError):
        BatchBudget(token_budget=1, slot_budget=1, prefill_chunk_budget=-1)


def test_negative_lookahead_rejected() -> None:
    with pytest.raises(RouterConfigError):
        TokenBinPacker(lookahead=-1)


def test_total_tokens_helper() -> None:
    assert total_tokens([_req("a", prompt=10, out=5), _req("b", prompt=20)]) == 35
