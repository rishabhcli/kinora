"""Cold-start / warm-pool optimisation for video providers.

Kinora generates film a few seconds *ahead* of the reader, so the latency of the
**first** request to a video provider — the auth handshake, connection setup,
signed-session minting — is the enemy. This subsystem hides that cold-start cost
behind a per-provider pool of warm, reusable sessions that the render path borrows
and returns under a fair lease, kept "just warm enough" by a cost-aware pre-warm
scheduler driven by predicted near-term demand (reader velocity / scheduler hint).

Components
----------
* :class:`~app.video.warmpool.clock.VirtualClock` / :class:`~app.video.warmpool.clock.SystemClock`
  — the injectable time + async-sleep seam (FINAL round: local, no cross-round import).
* :class:`~app.video.warmpool.protocols.SessionFactory` /
  :class:`~app.video.warmpool.protocols.ProviderSession` — the only I/O seams; the
  pool is pure logic over them (real factory in prod, fake in tests).
* :class:`~app.video.warmpool.cost.ColdStartModel` — measured first-vs-warm latency
  per provider; its conservative cold-start figure sizes the warm target.
* :class:`~app.video.warmpool.demand.DemandModel` — predicted near-term demand →
  cost-aware warm target (clamped into the pool bounds).
* :class:`~app.video.warmpool.pool.ProviderPool` — one provider's pool: min-warm
  maintenance, idle eviction, health-checked recycling, fair borrow/return with
  timeout + exhaustion back-pressure, circuit-aware drain, no warm-session leak.
* :class:`~app.video.warmpool.manager.WarmPoolManager` — multi-provider owner +
  the keep-alive / pre-warm scheduler loop.

The whole subsystem manages **connections**, not renders: it never calls a video
provider's ``render`` and never touches the ``KINORA_LIVE_VIDEO`` spend gate.
"""

from __future__ import annotations

from .clock import SYSTEM_CLOCK, Clock, SystemClock, VirtualClock
from .cost import ColdStartModel, LatencyStats
from .demand import DemandModel
from .lease import (
    FairWaiterQueue,
    Lease,
    LeaseError,
    LeaseTimeout,
    PoolDraining,
)
from .manager import WarmPoolManager
from .pool import PoolStats, ProviderPool
from .protocols import HealthSignal, ProviderId, ProviderSession, SessionFactory
from .settings import WarmPoolConfig

__all__ = [
    "SYSTEM_CLOCK",
    "Clock",
    "ColdStartModel",
    "DemandModel",
    "FairWaiterQueue",
    "HealthSignal",
    "LatencyStats",
    "Lease",
    "LeaseError",
    "LeaseTimeout",
    "PoolDraining",
    "PoolStats",
    "ProviderId",
    "ProviderPool",
    "ProviderSession",
    "SessionFactory",
    "SystemClock",
    "VirtualClock",
    "WarmPoolConfig",
    "WarmPoolManager",
]
