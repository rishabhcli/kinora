"""Connection proxy / pooler: a pgbouncer-shaped pool in front of each shard.

A shard's Postgres has a hard ceiling on backend connections, but a fleet of API
and worker processes wants far more *logical* connections than that. The proxy
multiplexes many logical clients over a small, bounded set of server connections
— exactly pgbouncer's job — so the shard never sees more than ``pool_size``
backends no matter how many callers there are.

Pooling mode (we implement the most aggressive useful one):

* **Transaction pooling.** A client borrows a backend only for the duration of
  one transaction and returns it the instant the transaction ends. Between
  transactions a client holds *no* backend, so N clients share P backends with
  P ≪ N. (Contrast *session pooling*, which pins a backend for the client's whole
  session — far less sharing. *Statement pooling* is finer but breaks
  multi-statement transactions, so we don't.)

The proxy is an async, fair pool:

* **Bounded wait queue.** When all backends are checked out, an acquirer waits on
  a FIFO queue (fairness — first waiter served first) up to ``acquire_timeout_s``,
  then raises :class:`PoolTimeout`. The queue is itself bounded
  (``max_waiters``); past that, acquisition fails fast with :class:`PoolExhausted`
  rather than letting an unbounded backlog accumulate (the backpressure signal).
* **Health & lifecycle.** Backends are health-checked on acquire (``pre_ping``)
  and recycled past ``max_lifetime_s``; a backend that fails its check is
  discarded and replaced. A :class:`ProxyStats` snapshot mirrors
  :class:`app.db.health.PoolStats` so the observability panel reads both the
  same way.

The actual backend is abstracted behind :class:`BackendConnection` /
:class:`BackendFactory` so the pool's *mechanics* (fairness, bounds, timeouts,
recycling) are proven deterministically with fake connections;
:class:`EngineBackendFactory` binds it to a real shard engine.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger("app.datascale.sharding.proxy")


class PoolError(RuntimeError):
    """Base class for connection-proxy errors."""


class PoolTimeout(PoolError):  # noqa: N818 - pgbouncer/asyncio-idiomatic name
    """Raised when an acquire waits past ``acquire_timeout_s`` for a backend."""


class PoolExhausted(PoolError):  # noqa: N818 - intentional pool-state name
    """Raised when the wait queue is already full (fail-fast backpressure)."""


class PoolClosed(PoolError):  # noqa: N818 - intentional pool-state name
    """Raised when acquiring from a closed pool."""


class BackendConnection(Protocol):
    """One server connection to a shard (what the proxy multiplexes).

    Production wraps a SQLAlchemy ``AsyncConnection`` on the shard's engine;
    tests use an in-memory fake. The proxy calls :meth:`ping` on acquire (when
    ``pre_ping``) and :meth:`close` on recycle/shutdown. ``begin``/``commit``/
    ``rollback`` bracket the transaction-pooling lease.
    """

    @property
    def id(self) -> int: ...

    async def ping(self) -> bool:
        """Cheap liveness check (``SELECT 1``); False ⇒ discard this backend."""
        ...

    async def begin(self) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


#: Builds a fresh :class:`BackendConnection` (opens one server connection).
BackendFactory = Callable[[], Awaitable[BackendConnection]]


@dataclass(slots=True)
class ProxyConfig:
    """Bounds + lifecycle knobs for one shard's proxy pool."""

    #: Maximum concurrent *server* connections to the shard.
    pool_size: int = 10
    #: Maximum acquirers that may wait when the pool is full (then fail fast).
    max_waiters: int = 100
    #: How long an acquire blocks for a free backend before :class:`PoolTimeout`.
    acquire_timeout_s: float = 5.0
    #: Recycle a backend older than this (0 = never recycle on age).
    max_lifetime_s: float = 1800.0
    #: Health-check a backend on acquire; a failed check discards + replaces it.
    pre_ping: bool = True

    def __post_init__(self) -> None:
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        if self.max_waiters < 0:
            raise ValueError("max_waiters must be >= 0")
        if self.acquire_timeout_s <= 0:
            raise ValueError("acquire_timeout_s must be > 0")
        if self.max_lifetime_s < 0:
            raise ValueError("max_lifetime_s must be >= 0")


@dataclass(slots=True)
class ProxyStats:
    """A point-in-time snapshot of one proxy pool (mirrors ``db.health.PoolStats``)."""

    pool_size: int
    open_backends: int
    checked_out: int
    idle: int
    waiters: int
    total_acquired: int
    total_timeouts: int
    total_recycled: int
    total_health_discards: int

    @property
    def utilization(self) -> float:
        """Fraction of ``pool_size`` currently checked out."""
        if not self.pool_size:
            return 0.0
        return round(self.checked_out / self.pool_size, 4)

    @property
    def is_saturated(self) -> bool:
        """True when every backend slot is checked out."""
        return self.checked_out >= self.pool_size

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view for the metrics surface (§12.5)."""
        return {
            "pool_size": self.pool_size,
            "open_backends": self.open_backends,
            "checked_out": self.checked_out,
            "idle": self.idle,
            "waiters": self.waiters,
            "utilization": self.utilization,
            "is_saturated": self.is_saturated,
            "total_acquired": self.total_acquired,
            "total_timeouts": self.total_timeouts,
            "total_recycled": self.total_recycled,
            "total_health_discards": self.total_health_discards,
        }


@dataclass(slots=True)
class _PooledBackend:
    """Internal wrapper tracking a backend's age for recycling."""

    conn: BackendConnection
    created_at: float

    def expired(self, max_lifetime_s: float, now: float) -> bool:
        return max_lifetime_s > 0 and (now - self.created_at) >= max_lifetime_s


class ConnectionProxy:
    """A fair, bounded, transaction-pooling proxy in front of one shard.

    Use :meth:`transaction` for the common case — it acquires a backend, runs the
    body inside ``begin``/``commit`` (rolling back + still returning the backend
    on error), and releases. :meth:`acquire` is the lower-level lease for callers
    that manage their own transaction bracketing.

    The pool lazily opens backends up to ``pool_size``; it never opens more, so
    the shard's connection ceiling is respected regardless of client count. Idle
    backends are reused LIFO (the warmest connection first). A FIFO waiter queue
    keeps acquisition fair under contention.
    """

    def __init__(self, factory: BackendFactory, config: ProxyConfig | None = None) -> None:
        self._factory = factory
        self._config = config or ProxyConfig()
        self._idle: list[_PooledBackend] = []  # LIFO stack of available backends
        self._checked_out: dict[int, _PooledBackend] = {}
        self._open_count = 0  # idle + checked_out (backends that physically exist)
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._closed = False
        self._lock = asyncio.Lock()
        # counters
        self._total_acquired = 0
        self._total_timeouts = 0
        self._total_recycled = 0
        self._total_health_discards = 0

    @property
    def config(self) -> ProxyConfig:
        return self._config

    # -- public API ---------------------------------------------------------- #

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[BackendConnection]:
        """Lease a backend for one transaction (begin → body → commit/rollback).

        This is the transaction-pooling contract: the backend is held only for
        the transaction and returned the moment it ends, so other clients reuse
        it immediately. On an exception the transaction is rolled back and the
        backend is *still* returned to the pool (a clean rollback leaves it
        reusable).
        """
        conn = await self.acquire()
        try:
            await conn.begin()
            yield conn
            await conn.commit()
        except Exception:
            await self._safe_rollback(conn)
            raise
        finally:
            await self.release(conn)

    async def acquire(self) -> BackendConnection:
        """Check out a healthy backend, waiting (fairly, bounded) if none free."""
        if self._closed:
            raise PoolClosed("connection proxy is closed")
        deadline = time.monotonic() + self._config.acquire_timeout_s
        while True:
            backend = await self._try_take_or_open()
            if backend is not None:
                healthy = await self._ensure_healthy(backend)
                if healthy is None:
                    # Discarded an unhealthy/expired backend; loop to get another.
                    continue
                self._checked_out[healthy.conn.id] = healthy
                self._total_acquired += 1
                return healthy.conn
            # Pool full: wait on the fair FIFO queue.
            await self._wait_for_release(deadline)

    async def release(self, conn: BackendConnection) -> None:
        """Return a checked-out backend to the idle set and wake one waiter."""
        async with self._lock:
            pooled = self._checked_out.pop(conn.id, None)
            if pooled is None:
                # Releasing an unknown/foreign connection: close it defensively.
                await conn.close()
                self._open_count = max(0, self._open_count - 1)
            elif self._closed:
                await self._discard(pooled)
            else:
                self._idle.append(pooled)
            self._wake_one_waiter()

    async def close(self) -> None:
        """Close the pool: reject new acquires, close idle backends, wake waiters."""
        async with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
            for pooled in idle:
                await self._discard(pooled)
            while self._waiters:
                fut = self._waiters.popleft()
                if not fut.done():
                    fut.set_exception(PoolClosed("connection proxy closed"))

    def stats(self) -> ProxyStats:
        """A snapshot of the pool's current occupancy + lifetime counters."""
        return ProxyStats(
            pool_size=self._config.pool_size,
            open_backends=self._open_count,
            checked_out=len(self._checked_out),
            idle=len(self._idle),
            waiters=len(self._waiters),
            total_acquired=self._total_acquired,
            total_timeouts=self._total_timeouts,
            total_recycled=self._total_recycled,
            total_health_discards=self._total_health_discards,
        )

    # -- internals ----------------------------------------------------------- #

    async def _try_take_or_open(self) -> _PooledBackend | None:
        """Take an idle backend, or open a new one if under ``pool_size``.

        Returns ``None`` when the pool is at capacity and nothing is idle (the
        caller must then wait). Opening a backend is done outside the lock (it
        may block on a socket) but the capacity reservation is made under it so
        we never exceed ``pool_size`` under concurrency.
        """
        async with self._lock:
            if self._closed:
                raise PoolClosed("connection proxy is closed")
            if self._idle:
                return self._idle.pop()  # LIFO: warmest backend
            if self._open_count < self._config.pool_size:
                self._open_count += 1  # reserve the slot before opening
                reserved = True
            else:
                return None
        if reserved:
            try:
                conn = await self._factory()
            except Exception:
                async with self._lock:
                    self._open_count = max(0, self._open_count - 1)
                raise
            return _PooledBackend(conn=conn, created_at=time.monotonic())
        return None  # pragma: no cover - unreachable

    async def _ensure_healthy(self, pooled: _PooledBackend) -> _PooledBackend | None:
        """Recycle-on-age and pre-ping a backend; replace it if unhealthy.

        Returns the backend to hand out, a *fresh* replacement, or ``None`` if it
        was discarded and the caller should retry (e.g. capacity freed for a
        different waiter). Replacement keeps the reserved capacity slot, so the
        pool size is preserved.
        """
        now = time.monotonic()
        if pooled.expired(self._config.max_lifetime_s, now):
            self._total_recycled += 1
            return await self._replace(pooled)
        if self._config.pre_ping:
            try:
                alive = await pooled.conn.ping()
            except Exception:  # noqa: BLE001 - a failed ping means discard
                alive = False
            if not alive:
                self._total_health_discards += 1
                return await self._replace(pooled)
        return pooled

    async def _replace(self, pooled: _PooledBackend) -> _PooledBackend | None:
        """Close a dead/expired backend and open a fresh one in its slot."""
        await self._close_conn(pooled.conn)
        try:
            conn = await self._factory()
        except Exception:
            async with self._lock:
                self._open_count = max(0, self._open_count - 1)
                self._wake_one_waiter()
            return None
        return _PooledBackend(conn=conn, created_at=time.monotonic())

    async def _wait_for_release(self, deadline: float) -> None:
        """Block on the FIFO waiter queue until woken or the deadline passes."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._total_timeouts += 1
            raise PoolTimeout(
                f"timed out acquiring a backend after {self._config.acquire_timeout_s}s"
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            if len(self._waiters) >= self._config.max_waiters:
                raise PoolExhausted(
                    f"wait queue full ({self._config.max_waiters} waiters); "
                    "refusing to queue further (backpressure)"
                )
            self._waiters.append(fut)
        try:
            await asyncio.wait_for(fut, timeout=remaining)
        except TimeoutError as exc:
            self._total_timeouts += 1
            async with self._lock:
                self._remove_waiter(fut)
            raise PoolTimeout(
                f"timed out acquiring a backend after {self._config.acquire_timeout_s}s"
            ) from exc

    def _wake_one_waiter(self) -> None:
        """Resolve the next pending waiter's future (FIFO fairness). Lock held."""
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                return

    def _remove_waiter(self, fut: asyncio.Future[None]) -> None:
        """Drop a (timed-out) waiter from the queue. Lock held."""
        with suppress(ValueError):
            self._waiters.remove(fut)

    async def _discard(self, pooled: _PooledBackend) -> None:
        """Close a backend and free its capacity slot. Lock held."""
        await self._close_conn(pooled.conn)
        self._open_count = max(0, self._open_count - 1)

    async def _close_conn(self, conn: BackendConnection) -> None:
        try:
            await conn.close()
        except Exception as exc:  # noqa: BLE001 - close is best-effort
            logger.warning("proxy.close_failed", error=str(exc))

    async def _safe_rollback(self, conn: BackendConnection) -> None:
        try:
            await conn.rollback()
        except Exception as exc:  # noqa: BLE001 - rollback is best-effort
            logger.warning("proxy.rollback_failed", error=str(exc))


@dataclass(slots=True)
class ShardProxyPool:
    """A registry of one :class:`ConnectionProxy` per shard.

    The router resolves a shard id; this turns that id into the shard's pooled
    backend. Proxies are created lazily from a per-shard :class:`BackendFactory`
    and disposed together on :meth:`close`. This is the object the cross-shard
    executor would hold in production to get a connection for each shard it fans
    out to.
    """

    factories: dict[str, BackendFactory]
    config: ProxyConfig = field(default_factory=ProxyConfig)
    _proxies: dict[str, ConnectionProxy] = field(default_factory=dict)

    def proxy_for(self, shard_id: str) -> ConnectionProxy:
        """Return (lazily creating) the proxy for ``shard_id``."""
        proxy = self._proxies.get(shard_id)
        if proxy is None:
            factory = self.factories.get(shard_id)
            if factory is None:
                raise KeyError(f"no backend factory registered for shard {shard_id!r}")
            proxy = ConnectionProxy(factory, self.config)
            self._proxies[shard_id] = proxy
        return proxy

    def transaction(self, shard_id: str) -> Any:
        """Open a transaction-pooled lease on ``shard_id`` (see ``ConnectionProxy``)."""
        return self.proxy_for(shard_id).transaction()

    def stats(self) -> dict[str, ProxyStats]:
        """Per-shard pool stats for the observability panel."""
        return {sid: proxy.stats() for sid, proxy in self._proxies.items()}

    async def close(self) -> None:
        """Close every created proxy (best-effort)."""
        for sid, proxy in list(self._proxies.items()):
            try:
                await proxy.close()
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                logger.warning("proxy.pool_close_failed", shard=sid, error=str(exc))
        self._proxies.clear()


__all__ = [
    "BackendConnection",
    "BackendFactory",
    "ConnectionProxy",
    "PoolClosed",
    "PoolError",
    "PoolExhausted",
    "PoolTimeout",
    "ProxyConfig",
    "ProxyStats",
    "ShardProxyPool",
]
