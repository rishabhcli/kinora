"""Pure, additive DR configuration — objectives + retention knobs.

A frozen pydantic model rather than a read off the network so the engine is
fully deterministic in tests. A thin :func:`from_settings` adapter pulls
overrides from the application :class:`~app.core.config.Settings` when an
``dr_*`` attribute is present, but defaults stand alone — nothing in
``app.core.config`` needs to change for this package to import or be tested.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DRConfig(BaseModel):
    """Recovery objectives + retention policy for the backup engine."""

    model_config = ConfigDict(frozen=True)

    # --- Recovery objectives (RPO/RTO accounting gates against these) ------- #
    #: Recovery Point Objective: maximum tolerable data-loss window (seconds).
    #: A scheduled-backup cadence at/under this keeps the achievable RPO green.
    rpo_target_s: float = Field(default=300.0, ge=0.0)
    #: Recovery Time Objective: maximum tolerable restore duration (seconds).
    rto_target_s: float = Field(default=900.0, ge=0.0)

    # --- Retention (the GC honours these; see app.dr.retention) ------------- #
    #: Keep at most this many *full* backups (oldest beyond it are GC-eligible).
    keep_full: int = Field(default=7, ge=1)
    #: Keep incrementals newer than the Nth-most-recent full; older incrementals
    #: whose parent chain is being retired are collected with their full.
    keep_incremental_chains: int = Field(default=2, ge=0)
    #: A backup younger than this is never collected regardless of count (a
    #: safety floor so an aggressive count policy cannot drop a just-made backup).
    min_retain_age_s: float = Field(default=0.0, ge=0.0)

    # --- Health thresholds -------------------------------------------------- #
    #: A backup fleet whose freshest snapshot is older than this is "overdue".
    overdue_after_s: float = Field(default=3600.0, ge=0.0)

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> DRConfig:
        """Build a :class:`DRConfig`, overlaying any ``dr_*`` settings present.

        Defaults stand alone; this only reads attributes that already exist on
        ``settings`` so the package needs no change to ``app.core.config``.
        """
        if settings is None:
            return cls()
        overrides: dict[str, Any] = {}
        for field_name in cls.model_fields:
            attr = f"dr_{field_name}"
            if hasattr(settings, attr):
                overrides[field_name] = getattr(settings, attr)
        return cls(**overrides)


__all__ = ["DRConfig"]
