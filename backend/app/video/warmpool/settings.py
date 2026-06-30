"""Tunables for the warm pool (additive; pure dataclass, no env reads here).

Kept as a frozen dataclass so the pool is constructed deterministically in tests
without touching :class:`app.core.config.Settings`. :meth:`WarmPoolConfig.from_settings`
maps the additive ``warmpool_*`` settings (see config.py) onto it when the app
wires a real pool, so production knobs flow through the standard pydantic-settings
path while tests stay hermetic.

Every default is conservative and **cost-aware**: a small min-warm floor, a hard
max-size cap, idle eviction that reclaims unused sessions, and a pre-warm horizon
short enough that we only warm sessions we expect to use within a few seconds —
the scheduler's lead time. None of these spend video-seconds; they manage
*connections*, which are free to hold but not free to keep forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WarmPoolConfig:
    """Bounds + cadences for one provider's warm pool (and pool-wide policy)."""

    #: Master switch. When ``False`` the manager is inert: it opens sessions
    #: strictly on demand (cold every time) and runs no keep-alive loop. OFF by
    #: default so adopting the package changes nothing until flipped on.
    enabled: bool = False

    # --- pool bounds ----------------------------------------------------- #
    #: Floor of warm (idle, ready) sessions the pool maintains per provider when
    #: enabled. The pre-warm scheduler may raise the *effective* target above this
    #: under demand; it never drops below it (unless the provider is draining).
    min_warm: int = 1
    #: Hard ceiling on *total* sessions per provider (warm + leased). Borrow blocks
    #: (fairly, with timeout) once this is reached — back-pressure, not unbounded growth.
    max_size: int = 4
    #: Upper bound the pre-warm scheduler may raise the warm target to. Caps how many
    #: idle sessions a demand spike can provoke (cost guard).
    max_warm: int = 3

    # --- idle eviction --------------------------------------------------- #
    #: A warm session unused for longer than this is closed (down to ``min_warm``).
    idle_ttl_s: float = 120.0

    # --- health-checked recycling --------------------------------------- #
    #: Re-probe a warm session that has been idle at least this long before handing
    #: it out / on the keep-alive sweep; a failed probe recycles (closes) it.
    health_check_interval_s: float = 30.0
    #: Hard maximum age for any session (warm or freshly returned). Past this the
    #: session is recycled even if healthy — bounds token/connection staleness.
    max_session_age_s: float = 600.0

    # --- pre-warm scheduler --------------------------------------------- #
    #: Keep-alive loop tick. Each tick: refresh demand → adjust warm target →
    #: evict idle → recycle stale → top up to target.
    keepalive_interval_s: float = 5.0
    #: How far ahead (seconds) the demand model projects when sizing the warm
    #: target — match the scheduler's render lead time.
    prewarm_horizon_s: float = 8.0
    #: A provider whose measured cold-start *savings* fall below this isn't worth
    #: holding idle sessions for; its warm floor drops to zero (cost-aware).
    warm_worth_threshold_s: float = 0.5

    # --- lease fairness -------------------------------------------------- #
    #: Default ceiling on how long ``borrow`` waits for a session before raising
    #: :class:`~app.video.warmpool.lease.LeaseTimeout`. Per-call override allowed.
    borrow_timeout_s: float = 10.0

    @classmethod
    def from_settings(cls, settings: Any) -> WarmPoolConfig:
        """Map the additive ``warmpool_*`` settings onto a config (production wiring).

        Falls back to a default instance's values for any setting the object does
        not expose, so the package is forward-compatible with a partial ``Settings``.
        """
        d = cls()  # default instance: read fallbacks off it (slots-friendly)
        return cls(
            enabled=bool(getattr(settings, "warmpool_enabled", d.enabled)),
            min_warm=int(getattr(settings, "warmpool_min_warm", d.min_warm)),
            max_size=int(getattr(settings, "warmpool_max_size", d.max_size)),
            max_warm=int(getattr(settings, "warmpool_max_warm", d.max_warm)),
            idle_ttl_s=float(getattr(settings, "warmpool_idle_ttl_s", d.idle_ttl_s)),
            health_check_interval_s=float(
                getattr(settings, "warmpool_health_check_interval_s", d.health_check_interval_s)
            ),
            max_session_age_s=float(
                getattr(settings, "warmpool_max_session_age_s", d.max_session_age_s)
            ),
            keepalive_interval_s=float(
                getattr(settings, "warmpool_keepalive_interval_s", d.keepalive_interval_s)
            ),
            prewarm_horizon_s=float(
                getattr(settings, "warmpool_prewarm_horizon_s", d.prewarm_horizon_s)
            ),
            warm_worth_threshold_s=float(
                getattr(settings, "warmpool_warm_worth_threshold_s", d.warm_worth_threshold_s)
            ),
            borrow_timeout_s=float(
                getattr(settings, "warmpool_borrow_timeout_s", d.borrow_timeout_s)
            ),
        )


__all__ = ["WarmPoolConfig"]
