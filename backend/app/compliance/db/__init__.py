"""ORM models for the compliance subsystem.

The model classes live in :mod:`app.compliance.db.models`. This package
``__init__`` is intentionally light — it does **not** eagerly import that module —
so that importing ``app.compliance.db`` never starts loading ``models.py`` before
its dependencies are ready. ``app.db.models.__init__`` imports the submodule
directly to register the tables on ``Base.metadata``; importing the names below
is supported for convenience and only triggers the (cycle-free) submodule load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.compliance.db.models import (
        ComplianceLedgerEntry,
        ConsentPolicy,
        ConsentRecord,
        DSAREvent,
        DSARRequest,
        LegalHold,
        RetentionRule,
    )

__all__ = [
    "ComplianceLedgerEntry",
    "ConsentPolicy",
    "ConsentRecord",
    "DSAREvent",
    "DSARRequest",
    "LegalHold",
    "RetentionRule",
]


def __getattr__(name: str) -> object:
    """Lazily resolve the model classes from :mod:`app.compliance.db.models`.

    Deferring the import to attribute-access time (PEP 562) breaks the import
    cycle: ``app.compliance.db.models`` → ``app.db.models.enums`` →
    ``app.db.models.__init__`` → ``app.compliance.db`` no longer re-enters a
    half-initialised module, because importing this package does no work.
    """
    if name in __all__:
        from app.compliance.db import models

        return getattr(models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
