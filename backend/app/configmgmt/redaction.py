"""Safe configuration introspection — ``redacted_dump`` and key classification.

Operators want to *see* the live configuration (a ``/config`` debug surface, a
support bundle, a startup log line) but the raw :class:`~app.core.config.Settings`
carries the DashScope key, the JWT secret, S3 credentials, webhook secrets, OAuth
client secrets, and so on. This module produces a structurally-faithful dump with
every secret value masked, reusing the *same* secret-key vocabulary as
:mod:`app.core.logging` so the redaction policy is consistent across logs and
introspection.

The masking is by **key name** (e.g. anything containing ``secret``/``api_key``/
``token``/``password``/``pepper``) plus an explicit set of known-sensitive
Settings fields that don't match the name heuristic (e.g. ``s3_access_key``).
Non-secret values pass through unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging import REDACTED, _is_sensitive_key

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "ALWAYS_REDACT_FIELDS",
    "is_secret_field",
    "redact_mapping",
    "redacted_dump",
]

#: Settings fields whose *names* don't trip the substring heuristic in
#: :mod:`app.core.logging` but which still carry credentials. Kept explicit so a
#: rename or new field is caught by review rather than silently leaking.
ALWAYS_REDACT_FIELDS: frozenset[str] = frozenset(
    {
        "s3_access_key",
        "s3_secret_key",
        "jwt_secret",
        "api_key_pepper",
        "minimax_api_key",
        "openai_api_key",
        "dashscope_api_key",
        "mcp_auth_token",
        "billing_webhook_secret",
        "analytics_salt",
        "integrations_encryption_key",
        "readwise_webhook_secret",
        "notion_webhook_secret",
        "notion_oauth_client_secret",
        "pocket_oauth_client_secret",
    }
)


def is_secret_field(name: str) -> bool:
    """True when a Settings field name denotes a secret (mask its value)."""
    return name in ALWAYS_REDACT_FIELDS or _is_sensitive_key(name)


def _redact_one(name: str, value: Any) -> Any:
    """Mask ``value`` when ``name`` is secret; otherwise pass it through."""
    if value is None:
        # None stays None so an operator can tell "unset" from "set-but-hidden".
        return None
    if is_secret_field(name):
        return REDACTED
    # ``mcp_client_scopes`` is a nested map keyed by bearer token; redact its
    # keys/values too rather than printing raw tokens.
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_one(name, item) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    return value


def redact_mapping(data: dict[Any, Any]) -> dict[Any, Any]:
    """Redact a mapping by key name (used for nested dict settings)."""
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and is_secret_field(key):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        else:
            out[key] = _redact_one(str(key), value)
    return out


def redacted_dump(settings: Settings) -> dict[str, Any]:
    """Return a JSON-friendly, secret-masked dump of ``settings``.

    Every field is preserved (structure is faithful) but secret values are
    replaced with ``[REDACTED]``. ``None`` secrets stay ``None`` so an operator
    can distinguish "unset" from "hidden". Safe to log, return from an API, or
    drop into a support bundle.
    """
    raw = settings.model_dump()
    return {name: _redact_one(name, value) for name, value in raw.items()}
