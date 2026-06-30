"""The typed audit taxonomy — *what* can be recorded and *by whom*.

Every consequential action in Kinora is one :class:`AuditCategory` performed by
one :class:`AuditActorKind`, with a verb drawn from :class:`AuditAction` and an
:class:`AuditSeverity`. Keeping the vocabulary closed (string enums) means the
query layer can filter on stable values, the hash-chain commits to a stable
projection, and a reviewer reading a 90-day export sees one consistent
vocabulary rather than free-text drift.

The vocabulary deliberately spans the whole product so a single trail explains a
clip end-to-end:

* **canon** mutations — the six agents writing to the versioned canon (§8);
* **arbitration** decisions — the Showrunner resolving an agent conflict (§7.2);
* **render** accept / degrade — the §9.7 per-shot state machine outcomes;
* **budget** spend — video-seconds / cost reserved or consumed;
* **auth** — login / lockout / token-family revocation;
* **config / flag** changes — a feature flag or setting flipped;
* **scheduler**, **ingest**, **moderation**, **system** — the supporting cast.

This module is pure (enums + helpers, no I/O), so the taxonomy is trivially
unit-testable and importable from anywhere without dragging in a DB.
"""

from __future__ import annotations

import enum


class AuditActorKind(enum.StrEnum):
    """Who performed the action.

    The three accountability classes the audit trail distinguishes. ``AGENT`` is
    one of the six crew agents mutating canon; ``USER`` is a human (reader /
    director / operator); ``SYSTEM`` is an automated process (scheduler,
    render-worker, ingest, retention sweeper) acting on no one's direct behalf.
    """

    AGENT = "agent"
    USER = "user"
    SYSTEM = "system"


class AuditCategory(enum.StrEnum):
    """The subsystem an audited action belongs to (the coarse filter axis)."""

    CANON = "canon"
    ARBITRATION = "arbitration"
    RENDER = "render"
    BUDGET = "budget"
    AUTH = "auth"
    CONFIG = "config"
    FLAG = "flag"
    SCHEDULER = "scheduler"
    INGEST = "ingest"
    MODERATION = "moderation"
    SYSTEM = "system"


class AuditAction(enum.StrEnum):
    """The verb describing the action (the fine filter axis).

    A closed set of past-tense / state-naming verbs covering the consequential
    actions across the product. ``OTHER`` is the explicit escape hatch so a new
    call site is never blocked on extending the enum — it still records, just
    under a generic verb (and the free-text ``reason`` carries the specifics).
    """

    # Canon (§8) -------------------------------------------------------------
    CANON_CREATED = "canon.created"
    CANON_UPDATED = "canon.updated"
    CANON_DELETED = "canon.deleted"
    CANON_BRANCHED = "canon.branched"
    CANON_MERGED = "canon.merged"

    # Arbitration (§7.2) -----------------------------------------------------
    ARBITRATION_OPENED = "arbitration.opened"
    ARBITRATION_RESOLVED = "arbitration.resolved"
    ARBITRATION_OVERRIDDEN = "arbitration.overridden"

    # Render (§9.7) ----------------------------------------------------------
    RENDER_PLANNED = "render.planned"
    RENDER_ACCEPTED = "render.accepted"
    RENDER_DEGRADED = "render.degraded"
    RENDER_REJECTED = "render.rejected"
    RENDER_REGENERATED = "render.regenerated"

    # Budget -----------------------------------------------------------------
    BUDGET_RESERVED = "budget.reserved"
    BUDGET_SPENT = "budget.spent"
    BUDGET_RELEASED = "budget.released"
    BUDGET_EXHAUSTED = "budget.exhausted"

    # Auth -------------------------------------------------------------------
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOCKED_OUT = "auth.locked_out"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"

    # Config / flags ---------------------------------------------------------
    CONFIG_CHANGED = "config.changed"
    FLAG_ENABLED = "flag.enabled"
    FLAG_DISABLED = "flag.disabled"
    FLAG_UPDATED = "flag.updated"

    # Scheduler / ingest / moderation ---------------------------------------
    SCHEDULER_PROMOTED = "scheduler.promoted"
    SCHEDULER_EVICTED = "scheduler.evicted"
    INGEST_STARTED = "ingest.started"
    INGEST_COMPLETED = "ingest.completed"
    INGEST_FAILED = "ingest.failed"
    MODERATION_FLAGGED = "moderation.flagged"
    MODERATION_CLEARED = "moderation.cleared"

    # Audit-internal (segment lifecycle) ------------------------------------
    SEGMENT_SEALED = "audit.segment_sealed"
    RETENTION_PRUNED = "audit.retention_pruned"

    # Escape hatch -----------------------------------------------------------
    OTHER = "other"


class AuditSeverity(enum.StrEnum):
    """How consequential the action is (drives alerting / triage, not control)."""

    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


#: The default severity each category records at when a call site does not
#: specify one. Auth and budget skew higher because their events are the ones a
#: security / finance reviewer reaches for first.
_DEFAULT_SEVERITY: dict[AuditCategory, AuditSeverity] = {
    AuditCategory.AUTH: AuditSeverity.NOTICE,
    AuditCategory.BUDGET: AuditSeverity.NOTICE,
    AuditCategory.ARBITRATION: AuditSeverity.NOTICE,
    AuditCategory.CONFIG: AuditSeverity.WARNING,
    AuditCategory.FLAG: AuditSeverity.WARNING,
    AuditCategory.MODERATION: AuditSeverity.WARNING,
}


def default_severity(category: AuditCategory) -> AuditSeverity:
    """The severity an event in ``category`` defaults to when none is given."""
    return _DEFAULT_SEVERITY.get(category, AuditSeverity.INFO)


#: Which category each action *belongs* to. Used to validate that a call site
#: pairs a coherent (category, action) and to derive a category when only the
#: action is known. The mapping is total over :class:`AuditAction`.
_ACTION_CATEGORY: dict[AuditAction, AuditCategory] = {
    AuditAction.CANON_CREATED: AuditCategory.CANON,
    AuditAction.CANON_UPDATED: AuditCategory.CANON,
    AuditAction.CANON_DELETED: AuditCategory.CANON,
    AuditAction.CANON_BRANCHED: AuditCategory.CANON,
    AuditAction.CANON_MERGED: AuditCategory.CANON,
    AuditAction.ARBITRATION_OPENED: AuditCategory.ARBITRATION,
    AuditAction.ARBITRATION_RESOLVED: AuditCategory.ARBITRATION,
    AuditAction.ARBITRATION_OVERRIDDEN: AuditCategory.ARBITRATION,
    AuditAction.RENDER_PLANNED: AuditCategory.RENDER,
    AuditAction.RENDER_ACCEPTED: AuditCategory.RENDER,
    AuditAction.RENDER_DEGRADED: AuditCategory.RENDER,
    AuditAction.RENDER_REJECTED: AuditCategory.RENDER,
    AuditAction.RENDER_REGENERATED: AuditCategory.RENDER,
    AuditAction.BUDGET_RESERVED: AuditCategory.BUDGET,
    AuditAction.BUDGET_SPENT: AuditCategory.BUDGET,
    AuditAction.BUDGET_RELEASED: AuditCategory.BUDGET,
    AuditAction.BUDGET_EXHAUSTED: AuditCategory.BUDGET,
    AuditAction.AUTH_LOGIN: AuditCategory.AUTH,
    AuditAction.AUTH_LOGOUT: AuditCategory.AUTH,
    AuditAction.AUTH_LOGIN_FAILED: AuditCategory.AUTH,
    AuditAction.AUTH_LOCKED_OUT: AuditCategory.AUTH,
    AuditAction.AUTH_TOKEN_REVOKED: AuditCategory.AUTH,
    AuditAction.AUTH_PASSWORD_CHANGED: AuditCategory.AUTH,
    AuditAction.CONFIG_CHANGED: AuditCategory.CONFIG,
    AuditAction.FLAG_ENABLED: AuditCategory.FLAG,
    AuditAction.FLAG_DISABLED: AuditCategory.FLAG,
    AuditAction.FLAG_UPDATED: AuditCategory.FLAG,
    AuditAction.SCHEDULER_PROMOTED: AuditCategory.SCHEDULER,
    AuditAction.SCHEDULER_EVICTED: AuditCategory.SCHEDULER,
    AuditAction.INGEST_STARTED: AuditCategory.INGEST,
    AuditAction.INGEST_COMPLETED: AuditCategory.INGEST,
    AuditAction.INGEST_FAILED: AuditCategory.INGEST,
    AuditAction.MODERATION_FLAGGED: AuditCategory.MODERATION,
    AuditAction.MODERATION_CLEARED: AuditCategory.MODERATION,
    AuditAction.SEGMENT_SEALED: AuditCategory.SYSTEM,
    AuditAction.RETENTION_PRUNED: AuditCategory.SYSTEM,
    AuditAction.OTHER: AuditCategory.SYSTEM,
}


def category_for_action(action: AuditAction) -> AuditCategory:
    """The category an action canonically belongs to."""
    return _ACTION_CATEGORY[action]


def is_coherent(category: AuditCategory, action: AuditAction) -> bool:
    """True when ``action`` belongs to ``category``.

    ``OTHER`` is the universal escape hatch and is coherent with *any* category
    so a generic call site is never rejected.
    """
    if action is AuditAction.OTHER:
        return True
    return _ACTION_CATEGORY[action] is category


__all__ = [
    "AuditAction",
    "AuditActorKind",
    "AuditCategory",
    "AuditSeverity",
    "category_for_action",
    "default_severity",
    "is_coherent",
]
