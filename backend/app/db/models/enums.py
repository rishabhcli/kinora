"""Enumerated column types shared across models.

Every enum is stored as a portable ``VARCHAR`` plus a named ``CHECK`` constraint
(``native_enum=False``) rather than a Postgres ``ENUM`` type. This keeps
migrations simple (no separate type to ``CREATE``/``ALTER``) and the values are
the lowercase strings from the design spec — :func:`str_enum` wires
``values_callable`` so the *value* (not the member name) is what hits the wire.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


class BookStatus(enum.StrEnum):
    """Lifecycle of an imported book."""

    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class EntityType(enum.StrEnum):
    """Kind of canon node."""

    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"


class ShotStatus(enum.StrEnum):
    """Per-shot state machine (kinora.md §9.7)."""

    PLANNED = "planned"
    KEYFRAMED = "keyframed"
    PROMOTED = "promoted"
    RENDERING = "rendering"
    QA = "qa"
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    CONFLICT = "conflict"


class SessionMode(enum.StrEnum):
    """Who drives the workspace: the video (viewer) or the reader (director)."""

    VIEWER = "viewer"
    DIRECTOR = "director"


class RenderPriority(enum.StrEnum):
    """Render-queue lane (kinora.md §4.9)."""

    COMMITTED = "committed"
    SPECULATIVE = "speculative"
    KEYFRAME = "keyframe"


class RenderJobStatus(enum.StrEnum):
    """Render-job lifecycle in the priority queue (kinora.md §12.1)."""

    QUEUED = "queued"
    RESERVED = "reserved"
    SUBMITTED = "submitted"
    POLLING = "polling"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    CANCELLED = "cancelled"
    DEADLETTER = "deadletter"


class MfaMethod(enum.StrEnum):
    """The kind of second factor configured on an account (kinora.md §6)."""

    TOTP = "totp"
    RECOVERY = "recovery"


class AuthEventType(enum.StrEnum):
    """Categories recorded in the security audit log (kinora.md §12)."""

    REGISTER = "register"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGIN_LOCKED = "login_locked"
    LOGOUT = "logout"
    LOGOUT_ALL = "logout_all"
    TOKEN_REFRESH = "token_refresh"
    TOKEN_REUSE = "token_reuse_detected"
    PASSWORD_CHANGE = "password_change"
    PASSWORD_RESET_REQUEST = "password_reset_request"
    PASSWORD_RESET = "password_reset"
    MFA_ENROLL = "mfa_enroll"
    MFA_ENABLE = "mfa_enable"
    MFA_DISABLE = "mfa_disable"
    MFA_CHALLENGE_SUCCESS = "mfa_challenge_success"
    MFA_CHALLENGE_FAILURE = "mfa_challenge_failure"
    RECOVERY_CODE_USED = "recovery_code_used"
    RECOVERY_CODES_REGENERATED = "recovery_codes_regenerated"
    API_KEY_CREATE = "api_key_create"
    API_KEY_REVOKE = "api_key_revoke"
    API_KEY_USED = "api_key_used"
    SESSION_REVOKE = "session_revoke"
    ROLE_GRANT = "role_grant"
    ROLE_REVOKE = "role_revoke"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_ENABLED = "account_enabled"


def str_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Build a VARCHAR+CHECK column type for ``enum_cls`` storing member values.

    Args:
        enum_cls: the Python :class:`enum.Enum` subclass.
        name: stable constraint name (feeds the ``ck_`` naming convention).
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )
