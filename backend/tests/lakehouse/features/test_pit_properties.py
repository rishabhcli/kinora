"""Property-based tests for point-in-time correctness — the anti-leakage core.

These are the most important tests in the feature store: they assert, over
thousands of Hypothesis-generated histories, the invariants the training join
must never violate. A failure here means silent label leakage in training sets.

Invariants checked:

* **No leakage** — the chosen value's event time is never after the request time.
* **TTL bound** — the chosen value is never at/older than ``request - ttl``.
* **As-of maximality** — the chosen value is the newest eligible one (matches a
  brute-force oracle), with the latest-arrival tie-break.
* **Order independence / determinism** — shuffling the stored rows never changes
  the result.
* **Monotonicity of revelation** — advancing the request time never *removes* an
  already-eligible value (it can only reveal newer ones), for an infinite TTL.
* **Default on miss** — when nothing qualifies, every feature is its default.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.lakehouse.features.pit import point_in_time_lookup
from app.lakehouse.features.rows import FeatureRow
from app.lakehouse.features.types import (
    Entity,
    FeatureSource,
    FeatureSpec,
    FeatureView,
    ValueType,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_KEY = {"user_id": "u1"}


def _view(ttl_minutes: int | None) -> FeatureView:
    return FeatureView(
        name="user_stats",
        entities=(Entity(name="user"),),
        features=(FeatureSpec(name="score", dtype=ValueType.FLOAT, default=-1.0),),
        source=FeatureSource(name="user_stats_src", created_field="created_at"),
        ttl=None if ttl_minutes is None else timedelta(minutes=ttl_minutes),
    )


def _at(minutes: float) -> datetime:
    return _EPOCH + timedelta(minutes=minutes)


# Strategy: a list of (event_minute, created_minute, value) observations for one key.
_observations = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=1000),  # event minute
        st.integers(min_value=0, max_value=1000),  # arrival (created) minute
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
    min_size=0,
    max_size=40,
)


def _rows_from(observations: list[tuple[int, int, float]]) -> list[FeatureRow]:
    return [
        FeatureRow(
            keys=dict(_KEY),
            values={"score": value},
            event_timestamp=_at(ev),
            created_timestamp=_at(cr),
        )
        for ev, cr, value in observations
    ]


def _payload_key(val: float) -> str:
    """Mirror of ``pit._payload_key`` for the single-feature ``score`` payload."""
    return json.dumps([["score", repr(val)]], separators=(",", ":"))


def _oracle(
    observations: list[tuple[int, int, float]], *, request_min: int, ttl_min: int | None
) -> tuple[float, int] | None:
    """Brute-force the expected (value, event_minute) the as-of join should pick.

    Mirrors the implementation's total ordering exactly: newest event time, then
    latest arrival, then a canonical payload-string tie-break (the determinism
    guarantee for rows fully tied on both timestamps).
    """
    eligible = [
        (ev, cr, val)
        for ev, cr, val in observations
        if ev <= request_min and (ttl_min is None or ev > request_min - ttl_min)
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda t: (t[0], t[1], _payload_key(t[2])))
    return best[2], best[0]


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    observations=_observations,
    request_min=st.integers(min_value=0, max_value=1000),
    ttl_min=st.one_of(st.none(), st.integers(min_value=1, max_value=2000)),
)
def test_pit_matches_oracle_and_never_leaks(
    observations: list[tuple[int, int, float]],
    request_min: int,
    ttl_min: int | None,
) -> None:
    view = _view(ttl_min)
    rows = _rows_from(observations)
    request_ts = _at(request_min)
    result = point_in_time_lookup(view, keys=_KEY, request_ts=request_ts, rows=rows)
    expected = _oracle(observations, request_min=request_min, ttl_min=ttl_min)

    if expected is None:
        # Default on miss.
        assert result.hit is False
        assert result.values["score"] == -1.0
        assert result.as_of is None
        return

    exp_value, exp_event_min = expected
    assert result.hit is True
    # No leakage: chosen event time never after the request.
    assert result.as_of is not None and result.as_of <= request_ts
    # TTL bound: never at/older than request - ttl.
    if ttl_min is not None:
        assert result.as_of > request_ts - timedelta(minutes=ttl_min)
    # As-of maximality: the chosen event time matches the oracle's newest eligible.
    assert result.as_of == _at(exp_event_min)
    assert result.values["score"] == exp_value


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    observations=_observations,
    request_min=st.integers(min_value=0, max_value=1000),
    ttl_min=st.one_of(st.none(), st.integers(min_value=1, max_value=2000)),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_pit_is_order_independent(
    observations: list[tuple[int, int, float]],
    request_min: int,
    ttl_min: int | None,
    seed: int,
) -> None:
    view = _view(ttl_min)
    rows = _rows_from(observations)
    request_ts = _at(request_min)
    base = point_in_time_lookup(view, keys=_KEY, request_ts=request_ts, rows=rows)

    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    other = point_in_time_lookup(view, keys=_KEY, request_ts=request_ts, rows=shuffled)

    assert base.values == other.values
    assert base.as_of == other.as_of
    assert base.hit == other.hit


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    observations=_observations,
    r1=st.integers(min_value=0, max_value=1000),
    delta=st.integers(min_value=0, max_value=1000),
)
def test_pit_monotone_revelation_infinite_ttl(
    observations: list[tuple[int, int, float]],
    r1: int,
    delta: int,
) -> None:
    """With no TTL, advancing the request time never loses an already-eligible value."""
    view = _view(None)
    rows = _rows_from(observations)
    early = point_in_time_lookup(view, keys=_KEY, request_ts=_at(r1), rows=rows)
    later = point_in_time_lookup(view, keys=_KEY, request_ts=_at(r1 + delta), rows=rows)
    if early.hit:
        # Later request must still hit, with an event time >= the earlier pick.
        assert later.hit
        assert later.as_of is not None and early.as_of is not None
        assert later.as_of >= early.as_of


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(observations=_observations, request_min=st.integers(min_value=0, max_value=1000))
def test_pit_future_values_never_selected(
    observations: list[tuple[int, int, float]],
    request_min: int,
) -> None:
    """A value strictly after the request time is invisible (the leakage guard)."""
    view = _view(None)
    rows = _rows_from(observations)
    request_ts = _at(request_min)
    result = point_in_time_lookup(view, keys=_KEY, request_ts=request_ts, rows=rows)
    if result.hit:
        assert result.as_of is not None and result.as_of <= request_ts
