"""Unit tests for the load scenarios (app.reliability.scenarios)."""

from __future__ import annotations

import pytest

from app.reliability.scenarios import (
    EP_CREATE_SESSION,
    EP_INTENT,
    EP_SEEK,
    SCENARIOS,
    cold_open,
    get_scenario,
    seek_thrash,
    skim_storm,
    steady_reader,
)
from app.reliability.transport import Response


def _bind(scenario, *, seed=1):  # type: ignore[no-untyped-def]
    return scenario.session(session_id="sess_abc", book_id="book_demo", seed=seed)


def test_prologue_opens_a_session() -> None:
    sess = _bind(steady_reader())
    pro = sess.prologue()
    assert pro.method == "POST"
    assert pro.path == "/sessions"
    assert pro.endpoint == EP_CREATE_SESSION
    assert pro.json == {"book_id": "book_demo", "focus_word": 0}


def test_steady_reader_emits_mostly_intent() -> None:
    sess = _bind(steady_reader(), seed=3)
    reqs = list(sess.requests(duration_s=30.0))
    assert reqs, "expected a request stream"
    endpoints = [r.endpoint for r in reqs]
    intent_frac = endpoints.count(EP_INTENT) / len(endpoints)
    # The steady reader is overwhelmingly intent updates.
    assert intent_frac > 0.8
    # Every intent request carries the §4.3 payload.
    for r in reqs:
        if r.endpoint == EP_INTENT:
            assert set(r.json) == {"focus_word", "velocity", "mode"}
            assert r.path == "/sessions/sess_abc/intent"


def test_seek_thrash_emits_seeks() -> None:
    sess = _bind(seek_thrash(), seed=5)
    reqs = list(sess.requests(duration_s=40.0))
    seeks = [r for r in reqs if r.endpoint == EP_SEEK]
    assert seeks, "seek_thrash should emit seek requests"
    for r in seeks:
        assert r.path == "/sessions/sess_abc/seek"
        assert "word" in r.json


def test_cold_open_is_pure_forward_intent() -> None:
    sess = _bind(cold_open(), seed=2)
    reqs = list(sess.requests(duration_s=20.0))
    assert reqs
    assert all(r.endpoint == EP_INTENT for r in reqs)
    # Focus word advances monotonically (no seeks, no pauses).
    words = [int(r.json["focus_word"]) for r in reqs]
    assert words == sorted(words)


def test_intent_success_predicate_accepts_429() -> None:
    sess = _bind(steady_reader(), seed=1)
    reqs = list(sess.requests(duration_s=10.0))
    intent = next(r for r in reqs if r.endpoint == EP_INTENT)
    # 429 (write rate-limit) is expected backpressure, not an error for intent.
    assert intent.is_ok(Response(status=429, elapsed_ms=1.0)) is True
    assert intent.is_ok(Response(status=200, elapsed_ms=1.0)) is True
    assert intent.is_ok(Response(status=500, elapsed_ms=1.0)) is False


def test_seek_success_predicate_is_strict() -> None:
    sess = _bind(seek_thrash(), seed=5)
    reqs = list(sess.requests(duration_s=40.0))
    seek = next(r for r in reqs if r.endpoint == EP_SEEK)
    assert seek.is_ok(Response(status=200, elapsed_ms=1.0)) is True
    # A seek does not get the 429 pass — a dropped seek is a real failure.
    assert seek.is_ok(Response(status=429, elapsed_ms=1.0)) is False


def test_deterministic_request_stream() -> None:
    a = list(_bind(skim_storm(), seed=9).requests(duration_s=25.0))
    b = list(_bind(skim_storm(), seed=9).requests(duration_s=25.0))
    assert [(r.endpoint, r.json) for r in a] == [(r.endpoint, r.json) for r in b]


def test_registry_round_trip() -> None:
    for name in SCENARIOS:
        scenario = get_scenario(name)
        assert scenario.name == name
        assert scenario.description


def test_get_scenario_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        get_scenario("does_not_exist")


def test_endpoint_labels_are_templated_not_concrete() -> None:
    # The report buckets by route template (bounded cardinality), not the live id.
    sess = _bind(steady_reader(), seed=1)
    reqs = list(sess.requests(duration_s=10.0))
    for r in reqs:
        assert "{id}" in r.endpoint
        assert "sess_abc" in r.path  # the concrete path carries the id
