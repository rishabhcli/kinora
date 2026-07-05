"""End-to-end §13 KPI tests through the factory-built layer.

These pin that the *declarative* metrics-as-code KPIs agree with the
authoritative pure §13 math in :mod:`app.eval.metrics` over the same data — the
whole point of the semantic layer is that it computes the same numbers the
pure math does, sliceable by book / role / time.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from app.eval.metrics import (
    accepted_footage_efficiency as pure_efficiency,
)
from app.eval.metrics import (
    regeneration_rate as pure_regen_rate,
)
from app.lakehouse.semantic.engine import InMemoryEngine
from app.lakehouse.semantic.factory import build_kinora_graph, build_kinora_layer
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.service import SemanticLayer
from app.lakehouse.semantic.types import OrderBy, TimeGrain


def _ts(d: int) -> datetime:
    return datetime(2026, 6, d, 12, tzinfo=UTC)


def _engine() -> InMemoryEngine:
    """Populate the factory's physical sources (fact_shots / fact_buffer / dim_books)."""
    shots = [
        # book_a day1: 4 shots, 2 rejected, 3 regens total, ccs [.9,.8,.95,.7]
        _shot("s1", "book_a", "showrunner", _ts(1), 5, True, 0, 0.90, 0.0),
        _shot("s2", "book_a", "showrunner", _ts(1), 5, False, 1, 0.80, 0.0),
        _shot("s3", "book_a", "generator", _ts(1), 5, True, 0, 0.95, 0.0),
        _shot("s4", "book_a", "generator", _ts(1), 5, False, 2, 0.70, 0.0),
        # book_b day2: 4 shots, 0 rejected, 1 regen, ccs [.92,.88,.6,.99]
        _shot("s5", "book_b", "showrunner", _ts(2), 5, True, 0, 0.92, 0.0),
        _shot("s6", "book_b", "showrunner", _ts(2), 5, True, 0, 0.88, 0.0),
        _shot("s7", "book_b", "generator", _ts(2), 5, True, 1, 0.60, 0.0),
        _shot("s8", "book_b", "generator", _ts(2), 5, True, 0, 0.99, 0.0),
    ]
    buffer = [
        {
            "sample_id": f"b{i}",
            "book_id": "book_a",
            "sampled_at": _ts(1),
            "above_low": i != 3,
            "stalled": i == 3,
        }
        for i in range(10)
    ]
    books = [
        {"book_id": "book_a", "title": "Aesop", "genre": "fable"},
        {"book_id": "book_b", "title": "Grimm", "genre": "fairy_tale"},
    ]
    engine = InMemoryEngine()
    for src, rows in (("fact_shots", shots), ("fact_buffer", buffer), ("dim_books", books)):
        engine.register(src, rows)
    # The join resolver references models by name too.
    engine.register("shots", shots)
    engine.register("buffer", buffer)
    engine.register("books", books)
    return engine


def _shot(
    sid: str,
    book: str,
    role: str,
    at: datetime,
    secs: int,
    accepted: bool,
    regens: int,
    ccs: float,
    usd: float,
) -> dict[str, object]:
    return {
        "shot_id": sid,
        "book_id": book,
        "agent_role": role,
        "mode": "committed",
        "rendered_at": at,
        "seconds": secs,
        "accepted": accepted,
        "regens": regens,
        "ccs": ccs,
        "usd": usd,
    }


def _layer() -> SemanticLayer:
    return build_kinora_layer(_engine())


# --------------------------------------------------------------------------- #
# Headline KPIs vs the authoritative pure math
# --------------------------------------------------------------------------- #


def test_accepted_footage_efficiency_matches_pure() -> None:
    out = _layer().query(MetricQuery.of("accepted_footage_efficiency"))
    declarative = out.result.rows[0]["accepted_footage_efficiency"]
    # total 40s, rejected 10s.
    assert math.isclose(declarative, pure_efficiency(total_seconds=40, rejected_seconds=10))
    assert math.isclose(declarative, 75.0)


def test_regen_rate_matches_pure() -> None:
    out = _layer().query(MetricQuery.of("regen_rate"))
    declarative = out.result.rows[0]["regen_rate"]
    assert math.isclose(declarative, pure_regen_rate(regens=4, total_shots=8))
    assert math.isclose(declarative, 0.5)


def test_ccs_mean() -> None:
    out = _layer().query(MetricQuery.of("ccs"))
    ccs_sum = 0.90 + 0.80 + 0.95 + 0.70 + 0.92 + 0.88 + 0.60 + 0.99
    assert math.isclose(out.result.rows[0]["ccs"], ccs_sum / 8)


def test_buffer_health_fraction() -> None:
    out = _layer().query(MetricQuery.of("buffer_health", "buffer_stalls"))
    assert math.isclose(out.result.rows[0]["buffer_health"], 9 / 10)
    assert out.result.rows[0]["buffer_stalls"] == 1


# --------------------------------------------------------------------------- #
# Sliced KPIs (the self-serve win)
# --------------------------------------------------------------------------- #


def test_efficiency_sliced_by_book() -> None:
    out = _layer().query(
        MetricQuery.of(
            "accepted_footage_efficiency",
            group_by=("book_id",),
            order_by=(OrderBy("book_id"),),
        )
    )
    by_book = {r["book_id"]: r["accepted_footage_efficiency"] for r in out.result.rows}
    # book_a: rejected 10 / total 20 -> 50%. book_b: 0 / 20 -> 100%.
    assert math.isclose(by_book["book_a"], 50.0)
    assert math.isclose(by_book["book_b"], 100.0)


def test_regen_rate_by_role() -> None:
    out = _layer().query(
        MetricQuery.of("regen_rate", group_by=("agent_role",), order_by=(OrderBy("agent_role"),))
    )
    by_role = {r["agent_role"]: r["regen_rate"] for r in out.result.rows}
    # generator: 3 regens / 4 shots = 0.75; showrunner: 1/4 = 0.25.
    assert math.isclose(by_role["generator"], 0.75)
    assert math.isclose(by_role["showrunner"], 0.25)


# --------------------------------------------------------------------------- #
# Budget burn (cumulative + period over period)
# --------------------------------------------------------------------------- #


def test_budget_burn_cumulative_over_days() -> None:
    out = _layer().query(
        MetricQuery.of(
            "usd_total",
            "budget_burn",
            time_grain=TimeGrain.DAY,
            time_dimension="rendered_at",
        )
    )
    # KINORA_LIVE_VIDEO off -> usd all 0 -> burn stays 0 (the gate is honoured).
    assert [r["budget_burn"] for r in out.result.rows] == [0.0, 0.0]


def test_factory_graph_validates() -> None:
    graph = build_kinora_graph()
    # All KPI metrics resolved + DAG acyclic (build() would have raised otherwise).
    assert "accepted_footage_efficiency" in graph.metrics
    assert "buffer_health" in graph.metrics
    assert graph.topo_order()  # non-empty, no cycle


def test_catalog_from_factory_tags_kpis() -> None:
    layer = _layer()
    headline = {m.name for m in layer.catalog.by_tag("headline")}
    assert "accepted_footage_efficiency" in headline
    assert "ccs" in headline
    assert "buffer_health" in headline
