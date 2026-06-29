"""Shared fixtures for the semantic / metrics-layer tests.

A tiny, fully in-memory star schema modelling Kinora render telemetry (the §13
KPI substrate) plus a books dimension to exercise joins. The numbers are chosen
so every KPI has a *known* closed-form answer the tests pin against.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.lakehouse.semantic.engine import InMemoryEngine
from app.lakehouse.semantic.model import (
    Dimension,
    Join,
    JoinType,
    Measure,
    SemanticModel,
)
from app.lakehouse.semantic.types import (
    Aggregation,
    Comparison,
    DataType,
    FieldRef,
    Predicate,
    TimeGrain,
)


def _ts(y: int, m: int, d: int, h: int = 12) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def shots_model() -> SemanticModel:
    """One row per rendered shot — the §13 fact table."""
    return SemanticModel(
        name="shots",
        source="fact_shots",
        primary_entity="shot_id",
        dimensions=(
            Dimension(name="shot_id", data_type=DataType.STRING),
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(name="agent_role", data_type=DataType.STRING),
            Dimension(name="mode", data_type=DataType.STRING),
            Dimension(
                name="rendered_at",
                data_type=DataType.TIMESTAMP,
                is_time=True,
                base_grain=TimeGrain.HOUR,
            ),
        ),
        measures=(
            Measure(name="shot_count", agg=Aggregation.COUNT, expr=None),
            Measure(name="total_seconds", agg=Aggregation.SUM, expr="seconds"),
            Measure(
                name="rejected_seconds",
                agg=Aggregation.SUM,
                expr="seconds",
                measure_filter=Predicate(
                    field=FieldRef(name="accepted"), op=Comparison.EQ, value=False
                ),
            ),
            Measure(
                name="regen_count",
                agg=Aggregation.SUM,
                expr="regens",
            ),
            Measure(
                name="video_seconds_spent",
                agg=Aggregation.SUM,
                expr="seconds",
                measure_filter=Predicate(
                    field=FieldRef(name="live"), op=Comparison.EQ, value=True
                ),
            ),
            Measure(name="ccs_sum", agg=Aggregation.SUM, expr="ccs"),
            Measure(name="usd_spent", agg=Aggregation.SUM, expr="usd"),
            Measure(
                name="accepted_shot_count",
                agg=Aggregation.SUM_BOOLEAN,
                expr="accepted",
            ),
        ),
        joins=(
            Join(
                to_model="books",
                from_key="book_id",
                to_key="book_id",
                join_type=JoinType.LEFT,
                many_to_one=True,
            ),
        ),
    )


def books_model() -> SemanticModel:
    """A small dimension table to exercise many-to-one joins."""
    return SemanticModel(
        name="books",
        source="dim_books",
        primary_entity="book_id",
        dimensions=(
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(name="title", data_type=DataType.STRING),
            Dimension(name="genre", data_type=DataType.STRING),
        ),
        measures=(Measure(name="book_count", agg=Aggregation.COUNT_DISTINCT, expr="book_id"),),
    )


def buffer_model() -> SemanticModel:
    """One row per buffer sample (for the buffer-health KPI)."""
    return SemanticModel(
        name="buffer",
        source="fact_buffer",
        primary_entity="sample_id",
        dimensions=(
            Dimension(name="sample_id", data_type=DataType.STRING),
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(
                name="sampled_at",
                data_type=DataType.TIMESTAMP,
                is_time=True,
                base_grain=TimeGrain.HOUR,
            ),
        ),
        measures=(
            Measure(name="sample_count", agg=Aggregation.COUNT, expr=None),
            Measure(name="above_low_count", agg=Aggregation.SUM_BOOLEAN, expr="above_low"),
            Measure(name="stall_count", agg=Aggregation.SUM_BOOLEAN, expr="stalled"),
        ),
    )


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def shot_rows() -> list[dict[str, object]]:
    """Eight shots across two books / two roles / two days.

    Known totals (overall):
      shot_count           = 8
      total_seconds        = 5+5+5+5 + 5+5+5+5 = 40
      rejected_seconds     = shots with accepted=False -> two 5s shots = 10
      regen_count          = 0+1+0+2 + 0+0+1+0 = 4
      ccs_sum              = 0.9+0.8+0.95+0.7 + 0.92+0.88+0.6+0.99 = 6.74
      accepted_shot_count  = 6 (two rejected)
      usd_spent            = 0 (KINORA_LIVE_VIDEO off; Ken-Burns is free)
    """
    return [
        # book_a, showrunner, day 1
        _shot("s1", "book_a", "showrunner", _ts(2026, 6, 1), 5, True, 0, 0.90, 0.0),
        _shot("s2", "book_a", "showrunner", _ts(2026, 6, 1), 5, False, 1, 0.80, 0.0),
        # book_a, generator, day 1
        _shot("s3", "book_a", "generator", _ts(2026, 6, 1), 5, True, 0, 0.95, 0.0),
        _shot("s4", "book_a", "generator", _ts(2026, 6, 1), 5, False, 2, 0.70, 0.0),
        # book_b, showrunner, day 2
        _shot("s5", "book_b", "showrunner", _ts(2026, 6, 2), 5, True, 0, 0.92, 0.0),
        _shot("s6", "book_b", "showrunner", _ts(2026, 6, 2), 5, True, 0, 0.88, 0.0),
        # book_b, generator, day 2
        _shot("s7", "book_b", "generator", _ts(2026, 6, 2), 5, True, 1, 0.60, 0.0),
        _shot("s8", "book_b", "generator", _ts(2026, 6, 2), 5, True, 0, 0.99, 0.0),
    ]


def _shot(
    shot_id: str,
    book_id: str,
    role: str,
    rendered_at: datetime,
    seconds: int,
    accepted: bool,
    regens: int,
    ccs: float,
    usd: float,
) -> dict[str, object]:
    return {
        "shot_id": shot_id,
        "book_id": book_id,
        "agent_role": role,
        "mode": "committed",
        "rendered_at": rendered_at,
        "seconds": seconds,
        "accepted": accepted,
        "regens": regens,
        "ccs": ccs,
        "usd": usd,
        "live": False,
    }


def book_rows() -> list[dict[str, object]]:
    return [
        {"book_id": "book_a", "title": "Aesop", "genre": "fable"},
        {"book_id": "book_b", "title": "Grimm", "genre": "fairy_tale"},
    ]


def buffer_rows() -> list[dict[str, object]]:
    # 10 samples; 9 above the low watermark, 1 stall.
    rows: list[dict[str, object]] = []
    for i in range(10):
        rows.append(
            {
                "sample_id": f"b{i}",
                "book_id": "book_a",
                "sampled_at": _ts(2026, 6, 1, h=10 + i),
                "above_low": i != 3,  # one sample below L
                "stalled": i == 3,  # one stall
            }
        )
    return rows


def make_engine() -> InMemoryEngine:
    """A populated in-memory engine registering every model under both keys."""
    engine = InMemoryEngine()
    # Register under physical source AND model name (the join resolver uses the
    # model name; see InMemoryEngine._model_source).
    engine.register("fact_shots", shot_rows())
    engine.register("shots", shot_rows())
    engine.register("dim_books", book_rows())
    engine.register("books", book_rows())
    engine.register("fact_buffer", buffer_rows())
    engine.register("buffer", buffer_rows())
    return engine


__all__ = [
    "book_rows",
    "books_model",
    "buffer_model",
    "buffer_rows",
    "make_engine",
    "shot_rows",
    "shots_model",
]
