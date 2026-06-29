"""Shared fixtures/builders for the feature-store test suite.

A small set of canonical feature definitions reused across the unit tests, plus
helpers to build offline rows and entity requests so each test reads as the
behaviour it asserts rather than boilerplate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.lakehouse.features import (
    Entity,
    FeatureRegistry,
    FeatureRow,
    FeatureService,
    FeatureSource,
    FeatureSpec,
    FeatureStore,
    FeatureView,
    ValueType,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def at(minutes: float) -> datetime:
    """A timestamp ``minutes`` after the test epoch."""
    return EPOCH + timedelta(minutes=minutes)


def user_stats_view(*, ttl_minutes: int | None = 60) -> FeatureView:
    return FeatureView(
        name="user_stats",
        entities=(Entity(name="user"),),
        features=(
            FeatureSpec(name="pages_read", dtype=ValueType.INT, default=0),
            FeatureSpec(name="avg_dwell_s", dtype=ValueType.FLOAT, default=0.0),
            FeatureSpec(name="genre", dtype=ValueType.STRING, default="unknown"),
        ),
        source=FeatureSource(name="user_stats_src", created_field="created_at"),
        ttl=None if ttl_minutes is None else timedelta(minutes=ttl_minutes),
        description="Per-user reading engagement features.",
    )


def book_features_view(*, ttl_minutes: int | None = None) -> FeatureView:
    return FeatureView(
        name="book_feats",
        entities=(Entity(name="book"),),
        features=(
            FeatureSpec(name="popularity", dtype=ValueType.FLOAT, default=0.0),
            FeatureSpec(
                name="embedding", dtype=ValueType.FLOAT_VECTOR, default=None
            ),
        ),
        source=FeatureSource(name="book_feats_src"),
        ttl=None if ttl_minutes is None else timedelta(minutes=ttl_minutes),
    )


def user_row(
    user_id: str,
    *,
    minute: float,
    pages: int,
    dwell: float,
    genre: str,
    created: float | None = None,
) -> FeatureRow:
    return FeatureRow(
        keys={"user_id": user_id},
        values={"pages_read": pages, "avg_dwell_s": dwell, "genre": genre},
        event_timestamp=at(minute),
        created_timestamp=None if created is None else at(created),
    )


def book_row(
    book_id: str, *, minute: float, popularity: float, embedding: list[float] | None
) -> FeatureRow:
    return FeatureRow(
        keys={"book_id": book_id},
        values={"popularity": popularity, "embedding": embedding},
        event_timestamp=at(minute),
    )


@pytest.fixture
def registry() -> FeatureRegistry:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    reg.register_feature_view(book_features_view())
    reg.register_feature_service(
        FeatureService(
            name="recsys_v1",
            features=(
                "user_stats:pages_read",
                "user_stats:avg_dwell_s",
                "book_feats:popularity",
            ),
        )
    )
    return reg


@pytest.fixture
def store() -> FeatureStore:
    fs = FeatureStore()
    fs.register_feature_view(user_stats_view())
    fs.register_feature_view(book_features_view())
    fs.register_feature_service(
        FeatureService(
            name="recsys_v1",
            features=(
                "user_stats:pages_read",
                "user_stats:avg_dwell_s",
                "book_feats:popularity",
            ),
        )
    )
    return fs
