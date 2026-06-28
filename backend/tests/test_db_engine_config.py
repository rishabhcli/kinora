"""Unit tests for the typed engine config + slow-query recorder (no infra).

These never open a connection: they exercise the pure mapping from
:class:`EngineConfig` to ``create_async_engine`` kwargs / asyncpg connect-args,
the pool-class selection, and the bounded slow-query ring buffer.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.db.engine import (
    EngineConfig,
    EngineRegistry,
    SlowQueryRecorder,
    build_engine,
    get_recorder,
)

_PG = "postgresql+asyncpg://u:p@localhost:5433/db"


def test_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        EngineConfig(url="")
    with pytest.raises(ValueError, match="pool_size"):
        EngineConfig(url=_PG, pool_size=-1)
    with pytest.raises(ValueError, match="max_overflow"):
        EngineConfig(url=_PG, max_overflow=-1)
    with pytest.raises(ValueError, match="pool_timeout_s"):
        EngineConfig(url=_PG, pool_timeout_s=0)
    with pytest.raises(ValueError, match="statement_timeout_ms"):
        EngineConfig(url=_PG, statement_timeout_ms=-5)


def test_asyncpg_detection_and_server_settings() -> None:
    cfg = EngineConfig(url=_PG, statement_timeout_ms=8000, application_name="kinora:api")
    assert cfg.is_asyncpg is True
    assert cfg.is_sqlite is False
    args = cfg.effective_connect_args()
    assert args["server_settings"]["statement_timeout"] == "8000"
    assert args["server_settings"]["application_name"] == "kinora:api"


def test_no_server_settings_when_unset() -> None:
    cfg = EngineConfig(url=_PG)
    assert cfg.effective_connect_args() == {}


def test_caller_connect_args_merge_not_clobber() -> None:
    cfg = EngineConfig(
        url=_PG,
        statement_timeout_ms=5000,
        connect_args={"server_settings": {"jit": "off"}, "timeout": 10},
    )
    args = cfg.effective_connect_args()
    # Derived statement_timeout coexists with the caller's jit override.
    assert args["server_settings"] == {"statement_timeout": "5000", "jit": "off"}
    assert args["timeout"] == 10


def test_pool_class_selection() -> None:
    from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

    assert EngineConfig(url=_PG).pool_class() is AsyncAdaptedQueuePool
    assert EngineConfig(url=_PG, use_null_pool=True).pool_class() is NullPool
    assert EngineConfig(url="sqlite+aiosqlite:///:memory:").pool_class() is NullPool


def test_create_kwargs_omits_pool_knobs_for_null_pool() -> None:
    null_kwargs = EngineConfig(url=_PG, use_null_pool=True).create_kwargs()
    assert "pool_size" not in null_kwargs
    assert "max_overflow" not in null_kwargs

    queue_kwargs = EngineConfig(url=_PG, pool_size=7, max_overflow=3).create_kwargs()
    assert queue_kwargs["pool_size"] == 7
    assert queue_kwargs["max_overflow"] == 3
    assert queue_kwargs["pool_pre_ping"] is True


def test_from_settings_maps_knobs() -> None:
    settings = Settings(
        dashscope_api_key="test",
        database_url=_PG,
        db_pool_size=5,
        db_max_overflow=15,
        db_statement_timeout_ms=3000,
        db_slow_query_ms=250.0,
    )
    cfg = EngineConfig.from_settings(settings, role="render-worker")
    assert cfg.pool_size == 5
    assert cfg.max_overflow == 15
    assert cfg.statement_timeout_ms == 3000
    assert cfg.slow_query_ms == 250.0
    assert cfg.application_name == "kinora:render-worker"


def test_build_engine_attaches_recorder() -> None:
    engine = build_engine(EngineConfig(url=_PG, slow_query_ms=100.0))
    recorder = get_recorder(engine)
    assert recorder is not None
    assert recorder.threshold_ms == 100.0


def test_build_engine_without_instrument() -> None:
    engine = build_engine(EngineConfig(url=_PG), instrument=False)
    assert get_recorder(engine) is None


def test_registry_replica_fallback_to_primary() -> None:
    reg = EngineRegistry(primary_config=EngineConfig(url=_PG))
    assert reg.has_replica is False
    # Reader falls back to the writer engine instance when no replica configured.
    assert reg.reader() is reg.writer()


def test_registry_distinct_replica() -> None:
    reg = EngineRegistry(
        primary_config=EngineConfig(url=_PG),
        replica_config=EngineConfig(url="postgresql+asyncpg://u:p@replica:5432/db"),
    )
    assert reg.has_replica is True
    assert reg.reader() is not reg.writer()
    assert reg.writer_built is True
    assert reg.replica_built is True


def test_registry_from_settings_wires_replica() -> None:
    settings = Settings(
        dashscope_api_key="test",
        database_url=_PG,
        database_replica_url="postgresql+asyncpg://u:p@replica:5432/db",
    )
    reg = EngineRegistry.from_settings(settings)
    assert reg.has_replica is True
    assert reg.replica_config is not None
    assert reg.replica_config.application_name == "kinora:replica"


def test_slow_query_recorder_threshold_and_ordering() -> None:
    rec = SlowQueryRecorder(threshold_ms=100.0, capacity=4)
    rec.observe("SELECT fast", 10.0)
    rec.observe("SELECT slow_a", 150.0, rowcount=3)
    rec.observe("SELECT slow_b", 500.0)
    rec.observe("SELECT slow_c", 250.0)
    snap = rec.snapshot()
    # Only the slow ones captured, and ordered slowest-first.
    assert [r.duration_ms for r in snap] == [500.0, 250.0, 150.0]
    assert snap[-1].rowcount == 3
    stats = rec.stats()
    assert stats["total_queries"] == 4
    assert stats["slow_queries"] == 3


def test_slow_query_recorder_ring_buffer_bound() -> None:
    rec = SlowQueryRecorder(threshold_ms=1.0, capacity=2)
    for i in range(5):
        rec.observe(f"SELECT {i}", float(10 + i))
    # Capacity bounds retained records to the most recent 2.
    assert rec.stats()["captured"] == 2
    rec.clear()
    assert rec.stats()["captured"] == 0


def test_slow_query_recorder_zero_threshold_captures_nothing() -> None:
    rec = SlowQueryRecorder(threshold_ms=0.0)
    rec.observe("SELECT x", 9999.0)
    assert rec.stats()["slow_queries"] == 0
    assert rec.snapshot() == []
