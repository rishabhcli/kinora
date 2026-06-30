"""Reader-session journey expansion + endpoint mix + think-time draws."""

from __future__ import annotations

import random

import pytest

from app.loadtest.scenario import (
    BUILTIN_SCENARIOS,
    ReadEndpoint,
    Scenario,
    Step,
    ThinkShape,
    ThinkTime,
    directing_reader,
    get_scenario,
    skimming_reader,
    steady_reader,
)


def test_steady_reader_journey_structure() -> None:
    sc = steady_reader(pages=4)
    rng = random.Random(0)
    reqs = list(sc.expand(rng, session_id="s1"))
    endpoints = [r.endpoint for r, _ in reqs]
    # open + 4 * (2 buffer + 1 page) + close = 1 + 12 + 1 = 14.
    assert len(reqs) == 14
    assert endpoints[0] == ReadEndpoint.OPEN_BOOK
    assert endpoints[-1] == ReadEndpoint.CLOSE
    assert endpoints.count(ReadEndpoint.PAGE_TURN) == 4
    assert endpoints.count(ReadEndpoint.BUFFER_STATE) == 8
    # session_id is woven into each payload.
    assert all(r.payload.get("session_id") == "s1" for r, _ in reqs)


def test_requests_per_journey_matches_expansion() -> None:
    sc = steady_reader(pages=5)
    rng = random.Random(1)
    assert sc.requests_per_journey() == len(list(sc.expand(rng)))


def test_endpoint_mix_sums_to_one() -> None:
    sc = steady_reader(pages=10)
    mix = sc.endpoint_mix()
    assert sum(mix.values()) == pytest.approx(1.0)
    # Buffer polls dominate a steady read.
    assert mix[ReadEndpoint.BUFFER_STATE] > mix[ReadEndpoint.PAGE_TURN]


def test_skimming_reader_is_jump_heavy() -> None:
    sc = skimming_reader(jumps=6)
    mix = sc.endpoint_mix()
    assert mix[ReadEndpoint.JUMP] > 0
    rng = random.Random(2)
    endpoints = [r.endpoint for r, _ in sc.expand(rng)]
    assert endpoints.count(ReadEndpoint.JUMP) == 6


def test_directing_reader_includes_comments() -> None:
    sc = directing_reader(pages=8, notes=3)
    rng = random.Random(3)
    endpoints = [r.endpoint for r, _ in sc.expand(rng)]
    assert endpoints.count(ReadEndpoint.COMMENT) == 3
    assert endpoints.count(ReadEndpoint.PAGE_TURN) == 8


def test_think_time_shapes_are_deterministic_and_bounded() -> None:
    fixed = ThinkTime(2.0, ThinkShape.FIXED)
    rng = random.Random(0)
    assert fixed.draw(rng) == 2.0

    uniform = ThinkTime(10.0, ThinkShape.UNIFORM, spread=0.5)
    rng = random.Random(0)
    draws = [uniform.draw(rng) for _ in range(1000)]
    assert all(5.0 <= d <= 15.0 for d in draws)

    exp = ThinkTime(4.0, ThinkShape.EXPONENTIAL)
    rng_a = random.Random(7)
    rng_b = random.Random(7)
    assert [exp.draw(rng_a) for _ in range(5)] == [exp.draw(rng_b) for _ in range(5)]


def test_expand_is_reproducible_for_seed() -> None:
    sc = steady_reader(pages=3)

    def run() -> list[float]:
        rng = random.Random(99)
        return [round(t, 6) for _, t in sc.expand(rng)]

    assert run() == run()


def test_builtin_registry_and_lookup() -> None:
    assert set(BUILTIN_SCENARIOS) == {
        "steady_reader",
        "skimming_reader",
        "directing_reader",
    }
    assert get_scenario("steady_reader").name == "steady_reader"
    with pytest.raises(KeyError):
        get_scenario("nope")


def test_scenario_validation() -> None:
    with pytest.raises(ValueError):
        Scenario(name="empty", steps=[])
    with pytest.raises(ValueError):
        Step(endpoint="x", count=0)
