"""Typed async-engine construction, pool tuning, and slow-query instrumentation.

The existing :mod:`app.db.session` keeps the simple, process-global
``get_engine``/``get_session`` helpers that most of the app uses. This module is
the *richer* foundation the rest of the DB-infrastructure layer (routing,
health, the generic repository, the inspector) binds to:

* :class:`EngineConfig` — a typed, validated bundle of pool/timeout knobs derived
  from :class:`app.core.config.Settings` (or built directly in tests). It maps to
  ``create_async_engine`` keyword arguments and to the Postgres ``connect_args``
  the asyncpg driver understands.
* :func:`build_engine` — construct one configured :class:`AsyncEngine`, optionally
  wiring the slow-query event listeners.
* :class:`EngineRegistry` — owns the *primary* engine and an optional
  *read-replica* engine, created lazily and disposed together. This is what the
  read/write split (:mod:`app.db.routing`) routes across.

Nothing here opens a socket at import or construction time: engines are created
on first access and connections only open on first query, so importing the
module (and building a registry) is safe with ``DASHSCOPE_API_KEY=test`` and no
infrastructure — exactly the contract :func:`app.composition.build_container`
relies on.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, Pool

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger("app.db.engine")

#: How many of the most-recent slow queries each engine keeps in memory. Bounded
#: so a long-running process never grows the ring buffer without limit; the
#: inspector (:mod:`app.db.inspect`) reads this for the observability panel.
SLOW_QUERY_RING_SIZE = 256


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Validated knobs for one async engine.

    Defaults reproduce the historical hard-coded behaviour of
    :func:`app.db.session.get_engine` (``pool_size=10``, ``max_overflow=20``,
    ``pool_pre_ping=True``) so adopting this builder changes nothing until a knob
    is set. ``statement_timeout_ms`` and ``slow_query_ms`` are *new* affordances
    (off / advisory-only by default).
    """

    url: str
    pool_size: int = 10
    max_overflow: int = 20
    pool_timeout_s: float = 30.0
    pool_recycle_s: int = 1800
    pool_pre_ping: bool = True
    #: Server-side ``statement_timeout`` in milliseconds (0 = no limit). Applied
    #: per-connection via asyncpg ``server_settings`` so a runaway query cannot
    #: pin a pool slot forever.
    statement_timeout_ms: int = 0
    #: Queries slower than this (seconds-equivalent of the configured ms) are
    #: logged and recorded in the engine's slow-query ring buffer.
    slow_query_ms: float = 500.0
    #: When true, use a :class:`NullPool` (no pooling). Tests and one-shot
    #: scripts want this so connections never linger between event loops.
    use_null_pool: bool = False
    #: SQL echo (debug only).
    echo: bool = False
    #: Extra ``connect_args`` merged last (wins over the derived asyncpg args).
    connect_args: Mapping[str, Any] = field(default_factory=dict)
    #: An optional application name reported to Postgres (``application_name``)
    #: so ``pg_stat_activity`` rows are attributable per role/engine.
    application_name: str | None = None

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("EngineConfig.url must be a non-empty database URL")
        if self.pool_size < 0:
            raise ValueError("pool_size must be >= 0")
        if self.max_overflow < 0:
            raise ValueError("max_overflow must be >= 0")
        if self.pool_timeout_s <= 0:
            raise ValueError("pool_timeout_s must be > 0")
        if self.statement_timeout_ms < 0:
            raise ValueError("statement_timeout_ms must be >= 0")
        if self.slow_query_ms < 0:
            raise ValueError("slow_query_ms must be >= 0")

    # -- derivations --------------------------------------------------------- #

    @property
    def is_asyncpg(self) -> bool:
        """True when the URL targets the asyncpg driver (Postgres async)."""
        return "+asyncpg" in self.url or self.url.startswith("postgresql+asyncpg")

    @property
    def is_sqlite(self) -> bool:
        """True for SQLite URLs (used by hermetic unit tests of the helpers)."""
        return self.url.startswith("sqlite")

    def server_settings(self) -> dict[str, str]:
        """Postgres per-connection ``server_settings`` (asyncpg) derived from knobs."""
        settings: dict[str, str] = {}
        if self.statement_timeout_ms > 0:
            settings["statement_timeout"] = str(self.statement_timeout_ms)
        if self.application_name:
            settings["application_name"] = self.application_name
        return settings

    def effective_connect_args(self) -> dict[str, Any]:
        """Resolve the ``connect_args`` passed to ``create_async_engine``.

        For asyncpg we fold the derived ``server_settings`` in; caller-supplied
        ``connect_args`` are merged last so they always win.
        """
        args: dict[str, Any] = {}
        if self.is_asyncpg:
            server_settings = self.server_settings()
            if server_settings:
                args["server_settings"] = server_settings
        # Caller overrides win; a caller-provided server_settings is merged, not
        # clobbered, so an explicit application_name plus a derived timeout coexist.
        for key, value in self.connect_args.items():
            if key == "server_settings" and isinstance(value, Mapping):
                merged = dict(args.get("server_settings", {}))
                merged.update(value)
                args["server_settings"] = merged
            else:
                args[key] = value
        return args

    def pool_class(self) -> type[Pool]:
        """Pick the pool implementation for this config."""
        if self.use_null_pool or self.is_sqlite:
            return NullPool
        return AsyncAdaptedQueuePool

    def create_kwargs(self) -> dict[str, Any]:
        """The full keyword-argument set for :func:`create_async_engine`."""
        kwargs: dict[str, Any] = {
            "future": True,
            "echo": self.echo,
            "pool_pre_ping": self.pool_pre_ping,
            "poolclass": self.pool_class(),
            "connect_args": self.effective_connect_args(),
        }
        # NullPool ignores size/overflow/timeout/recycle; passing them raises in
        # some SQLAlchemy versions, so only set them for a real queue pool.
        if kwargs["poolclass"] is AsyncAdaptedQueuePool:
            kwargs.update(
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_timeout=self.pool_timeout_s,
                pool_recycle=self.pool_recycle_s,
            )
        return kwargs

    # -- factory ------------------------------------------------------------- #

    @classmethod
    def from_settings(
        cls, settings: Settings, *, url: str | None = None, role: str | None = None
    ) -> EngineConfig:
        """Derive an :class:`EngineConfig` from application :class:`Settings`.

        ``url`` overrides the primary ``database_url`` (used to build the replica
        config). ``role`` becomes the connection's ``application_name`` suffix so
        ``api``/``render-worker``/``replica`` connections are distinguishable in
        ``pg_stat_activity``.
        """
        app_name = settings.service_name
        if role:
            app_name = f"{settings.service_name}:{role}"
        return cls(
            url=url or settings.database_url,
            pool_size=_setting(settings, "db_pool_size", 10),
            max_overflow=_setting(settings, "db_max_overflow", 20),
            pool_timeout_s=_setting(settings, "db_pool_timeout_s", 30.0),
            pool_recycle_s=_setting(settings, "db_pool_recycle_s", 1800),
            statement_timeout_ms=_setting(settings, "db_statement_timeout_ms", 0),
            slow_query_ms=_setting(settings, "db_slow_query_ms", 500.0),
            application_name=app_name,
        )

    def replace(self, **changes: Any) -> EngineConfig:
        """Return a copy with the given fields replaced (frozen-dataclass copy)."""
        return replace(self, **changes)


def _setting(settings: Settings, name: str, default: Any) -> Any:
    """Read an optional setting, falling back to ``default`` when absent.

    The pool/timeout knobs are *additive* to ``Settings``; this lets the engine
    builder work against an older ``Settings`` that predates them without raising.
    """
    return getattr(settings, name, default)


@dataclass(slots=True)
class SlowQueryRecord:
    """One captured slow statement (advisory; for the observability panel, §12.5)."""

    statement: str
    duration_ms: float
    at: float = field(default_factory=time.time)
    rowcount: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view for the metrics surface."""
        return {
            "statement": self.statement,
            "duration_ms": round(self.duration_ms, 3),
            "at": self.at,
            "rowcount": self.rowcount,
        }


class SlowQueryRecorder:
    """A bounded ring buffer of the slowest recent statements for one engine.

    The recorder is attached to an engine via :func:`attach_slow_query_listeners`,
    which times every ``cursor.execute`` and records statements over the
    configured threshold. It is the in-process feed the inspector exposes; it is
    *not* a substitute for ``pg_stat_statements`` (which the inspector also reads
    when the extension is installed).
    """

    def __init__(self, *, threshold_ms: float, capacity: int = SLOW_QUERY_RING_SIZE) -> None:
        self._threshold_ms = max(0.0, threshold_ms)
        self._records: deque[SlowQueryRecord] = deque(maxlen=capacity)
        self._lock = Lock()
        self._total_queries = 0
        self._slow_queries = 0

    @property
    def threshold_ms(self) -> float:
        """The slow-query threshold in milliseconds."""
        return self._threshold_ms

    def observe(self, statement: str, duration_ms: float, rowcount: int | None = None) -> None:
        """Record one executed statement, retaining it iff it crossed the threshold."""
        with self._lock:
            self._total_queries += 1
            if self._threshold_ms and duration_ms >= self._threshold_ms:
                self._slow_queries += 1
                self._records.append(
                    SlowQueryRecord(
                        statement=_truncate(statement),
                        duration_ms=duration_ms,
                        rowcount=rowcount,
                    )
                )

    def snapshot(self, *, limit: int | None = None) -> list[SlowQueryRecord]:
        """Return the slowest captured statements, slowest first."""
        with self._lock:
            ordered = sorted(self._records, key=lambda r: r.duration_ms, reverse=True)
        return ordered[:limit] if limit is not None else ordered

    def stats(self) -> dict[str, Any]:
        """Counters for the metrics panel: total/slow query counts + threshold."""
        with self._lock:
            return {
                "total_queries": self._total_queries,
                "slow_queries": self._slow_queries,
                "threshold_ms": self._threshold_ms,
                "captured": len(self._records),
            }

    def clear(self) -> None:
        """Drop all captured records (counters retained)."""
        with self._lock:
            self._records.clear()


def _truncate(statement: str, *, limit: int = 2000) -> str:
    """Collapse whitespace and cap a captured statement's length for logging."""
    collapsed = " ".join(statement.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


# Key under which a recorder is stashed on the sync ``Engine`` so it survives the
# async wrapper and can be fetched back by the inspector.
_RECORDER_ATTR = "_kinora_slow_query_recorder"


def attach_slow_query_listeners(engine: AsyncEngine, recorder: SlowQueryRecorder) -> None:
    """Time every statement on ``engine`` and feed slow ones to ``recorder``.

    Listens on the *sync* engine that backs the async one (SQLAlchemy's
    ``before_cursor_execute`` / ``after_cursor_execute`` fire on the sync layer).
    Idempotent: attaching twice is a no-op (the recorder is stored on the engine
    and re-attach is skipped).
    """
    sync_engine = engine.sync_engine
    if getattr(sync_engine, _RECORDER_ATTR, None) is not None:
        return
    setattr(sync_engine, _RECORDER_ATTR, recorder)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        context._kinora_query_start = time.perf_counter()  # noqa: SLF001

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        start = getattr(context, "_kinora_query_start", None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start) * 1000.0
        rowcount = getattr(cursor, "rowcount", None)
        recorder.observe(statement, duration_ms, rowcount if isinstance(rowcount, int) else None)
        if recorder.threshold_ms and duration_ms >= recorder.threshold_ms:
            logger.warning(
                "db.slow_query",
                duration_ms=round(duration_ms, 2),
                statement=_truncate(statement, limit=400),
            )


def get_recorder(engine: AsyncEngine) -> SlowQueryRecorder | None:
    """Return the slow-query recorder attached to ``engine`` (or ``None``)."""
    return getattr(engine.sync_engine, _RECORDER_ATTR, None)


def build_engine(config: EngineConfig, *, instrument: bool = True) -> AsyncEngine:
    """Construct one configured :class:`AsyncEngine`.

    When ``instrument`` is true a :class:`SlowQueryRecorder` is attached so the
    inspector and metrics panel see slow statements. Construction opens no
    connection (lazy ``create_async_engine``).
    """
    engine = create_async_engine(config.url, **config.create_kwargs())
    if instrument:
        attach_slow_query_listeners(engine, SlowQueryRecorder(threshold_ms=config.slow_query_ms))
    return engine


@dataclass(slots=True)
class EngineRegistry:
    """Owns the primary (writer) engine and an optional read-replica (reader).

    Engines are built lazily on first access and disposed together by
    :meth:`dispose`. When no replica is configured, :meth:`reader` returns the
    primary, so a caller can always "route reads to the reader" and get correct
    behaviour on a single-node deployment.
    """

    primary_config: EngineConfig
    replica_config: EngineConfig | None = None
    instrument: bool = True

    _primary: AsyncEngine | None = field(default=None, repr=False)
    _replica: AsyncEngine | None = field(default=None, repr=False)

    def writer(self) -> AsyncEngine:
        """The primary engine — all writes go here."""
        if self._primary is None:
            self._primary = build_engine(self.primary_config, instrument=self.instrument)
            logger.info(
                "db.engine.primary_built",
                pool_size=self.primary_config.pool_size,
                pre_ping=self.primary_config.pool_pre_ping,
            )
        return self._primary

    def reader(self) -> AsyncEngine:
        """The replica engine if configured, else the primary (safe fallback)."""
        if self.replica_config is None:
            return self.writer()
        if self._replica is None:
            self._replica = build_engine(self.replica_config, instrument=self.instrument)
            logger.info("db.engine.replica_built", pool_size=self.replica_config.pool_size)
        return self._replica

    @property
    def has_replica(self) -> bool:
        """True when a distinct read-replica engine is configured."""
        return self.replica_config is not None

    @property
    def writer_built(self) -> bool:
        """True once the primary engine has been constructed (no build side effect)."""
        return self._primary is not None

    @property
    def replica_built(self) -> bool:
        """True once the replica engine has been constructed (no build side effect)."""
        return self._replica is not None

    def engines(self) -> Iterator[tuple[str, AsyncEngine]]:
        """Yield ``(role, engine)`` for every *built* engine (for health probes)."""
        if self._primary is not None:
            yield "primary", self._primary
        if self._replica is not None:
            yield "replica", self._replica

    async def dispose(self) -> None:
        """Dispose every built engine's connection pool (call on shutdown)."""
        for role, engine in list(self.engines()):
            try:
                await engine.dispose()
            except Exception as exc:  # noqa: BLE001 - shutdown must be best-effort
                logger.warning("db.engine.dispose_failed", role=role, error=str(exc))
        self._primary = None
        self._replica = None

    @classmethod
    def from_settings(cls, settings: Settings, *, instrument: bool = True) -> EngineRegistry:
        """Build a registry from :class:`Settings`, wiring a replica when configured."""
        primary = EngineConfig.from_settings(settings, role="primary")
        replica_url = _setting(settings, "database_replica_url", None)
        replica = (
            EngineConfig.from_settings(settings, url=replica_url, role="replica")
            if replica_url
            else None
        )
        return cls(primary_config=primary, replica_config=replica, instrument=instrument)


__all__ = [
    "SLOW_QUERY_RING_SIZE",
    "EngineConfig",
    "EngineRegistry",
    "SlowQueryRecord",
    "SlowQueryRecorder",
    "attach_slow_query_listeners",
    "build_engine",
    "get_recorder",
]
