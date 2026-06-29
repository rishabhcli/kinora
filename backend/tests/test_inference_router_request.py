"""Tests for app.inference.router.request — the schedulable request unit.

These pin the content-free invariants the whole router relies on: validation at
construction, the token-footprint math, fair-share cost, coalesce-key default,
and the stable prefix-key hash.
"""

from __future__ import annotations

import pytest

from app.inference.router.errors import RouterConfigError
from app.inference.router.request import (
    TERMINAL_STATES,
    InferenceRequest,
    RequestPriority,
    RequestState,
    prefix_key_for,
)


def _req(**kw: object) -> InferenceRequest:
    base: dict[str, object] = {"request_id": "r1", "model": "m"}
    base.update(kw)
    return InferenceRequest(**base)  # type: ignore[arg-type]


def test_total_tokens_is_prompt_plus_output() -> None:
    r = _req(prompt_tokens=200, max_output_tokens=64)
    assert r.total_tokens == 264


def test_priority_ordering_is_strict() -> None:
    assert RequestPriority.INTERACTIVE > RequestPriority.COMMITTED
    assert RequestPriority.COMMITTED > RequestPriority.SPECULATIVE
    assert RequestPriority.SPECULATIVE > RequestPriority.BULK


def test_priority_from_name_is_case_insensitive() -> None:
    assert RequestPriority.from_name("committed") is RequestPriority.COMMITTED
    assert RequestPriority.from_name(" Speculative ") is RequestPriority.SPECULATIVE
    with pytest.raises(RouterConfigError):
        RequestPriority.from_name("nope")


def test_empty_request_id_rejected() -> None:
    with pytest.raises(RouterConfigError):
        InferenceRequest(request_id="", model="m")


def test_empty_model_rejected() -> None:
    with pytest.raises(RouterConfigError):
        InferenceRequest(request_id="r", model="")


def test_negative_tokens_rejected() -> None:
    with pytest.raises(RouterConfigError):
        _req(prompt_tokens=-1)
    with pytest.raises(RouterConfigError):
        _req(max_output_tokens=-5)


def test_non_positive_cost_weight_rejected() -> None:
    with pytest.raises(RouterConfigError):
        _req(cost_weight=0.0)
    with pytest.raises(RouterConfigError):
        _req(cost_weight=-2.0)


def test_non_positive_queue_sla_rejected() -> None:
    with pytest.raises(RouterConfigError):
        _req(queue_sla_s=0.0)


def test_effective_coalesce_key_defaults_to_request_id() -> None:
    assert _req().effective_coalesce_key == "r1"
    assert _req(coalesce_key="shared").effective_coalesce_key == "shared"


def test_fairness_key_is_tenant_agent_pair() -> None:
    r = _req(tenant="t1", agent="adapter")
    assert r.fairness_key() == ("t1", "adapter")


def test_share_cost_scales_with_weight_and_work() -> None:
    r = _req(prompt_tokens=100, max_output_tokens=0, cost_weight=2.0)
    assert r.share_cost() == pytest.approx(200.0)
    # Charging actual progress instead of the worst-case reservation.
    assert r.share_cost(tokens_done=10) == pytest.approx(20.0)


def test_share_cost_floors_at_one_token() -> None:
    r = _req(prompt_tokens=0, max_output_tokens=0)
    assert r.share_cost() == pytest.approx(1.0)


def test_with_enqueued_at_is_a_copy() -> None:
    r = _req()
    stamped = r.with_enqueued_at(12.5)
    assert stamped.enqueued_at == 12.5
    assert r.enqueued_at == 0.0  # original untouched (frozen dataclass)


def test_log_fields_never_leak_content() -> None:
    r = _req(tenant="t", agent="a", prompt_tokens=5, metadata={"secret": "x"})
    fields = r.as_log_fields()
    assert "metadata" not in fields
    assert "secret" not in str(fields)
    assert fields["prompt_tokens"] == 5


def test_prefix_key_is_stable_and_short() -> None:
    a = prefix_key_for("the same prefix")
    b = prefix_key_for("the same prefix")
    c = prefix_key_for("a different prefix")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_prefix_key_max_chars_truncates_input() -> None:
    # Two strings sharing the first N chars hash identically when truncated.
    long_a = "shared-head" + "A" * 100
    long_b = "shared-head" + "B" * 100
    assert prefix_key_for(long_a, max_chars=11) == prefix_key_for(long_b, max_chars=11)
    assert prefix_key_for(long_a) != prefix_key_for(long_b)


def test_terminal_states_cover_the_done_set() -> None:
    assert RequestState.SUCCEEDED in TERMINAL_STATES
    assert RequestState.QUEUED not in TERMINAL_STATES
    assert RequestState.RUNNING not in TERMINAL_STATES
