"""Log redaction: structlog processor + Secret wrapper.

Two cooperating pieces:

1. :func:`redact_log_event` — a structlog processor that masks values whose
   keys look like secrets and scrubs bearer tokens / ``sk-`` keys from
   free-text strings.  Designed to slot into the processor chain after event
   binding but before rendering.

2. :class:`Secret` — a typed wrapper whose ``__repr__`` and ``__str__`` always
   return ``"[REDACTED]"`` so a secret value never leaks through f-strings,
   ``logging.debug``, or exception tracebacks.  JSON serialization is
   explicitly refused to prevent accidental inclusion in API responses.

Both pieces are **pure** (no I/O, no network, no DB).  The processor is safe
to use in both sync and async contexts.

Relationship to ``app.core.logging``
--------------------------------------
``app.core.logging`` already ships a ``redact_secrets`` processor targeting the
existing logging pipeline.  This module is the *sechardening* namespace's own
production-grade version: it is self-contained (no import of
``app.core.logging``), extends the sensitive-key vocabulary, adds a configurable
key list, and provides the :class:`Secret` abstraction.  The two processors can
coexist in different pipeline positions, or one can replace the other.
"""

from __future__ import annotations

import json
import re
from typing import Any

from structlog.typing import EventDict, WrappedLogger

__all__ = [
    "REDACTED",
    "Secret",
    "SecretSerializationError",
    "redact_log_event",
    "is_sensitive_key",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel substituted for any redacted value.
REDACTED: str = "[REDACTED]"

#: Substrings that mark a key as carrying a secret.  Matching is done on the
#: lowercased key so ``ApiKey``, ``api_key``, and ``APIKEY`` all match
#: ``"apikey"``.
#:
#: ``"token"`` is matched *exactly* (see :func:`is_sensitive_key`) to avoid
#: tagging operational identifiers like ``cancel_token`` or ``task_id``.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "passwd",
    "secret",
    "bearer",
    "private_key",
    "client_secret",
    "webhook_secret",
    "signing_key",
    "credentials",
)

#: Keys matched exactly (case-insensitive) regardless of substrings.
_SENSITIVE_EXACT: frozenset[str] = frozenset({"token", "auth", "key"})

#: Pattern masking ``Bearer <value>`` in free-text strings.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")

#: Pattern masking ``sk-`` prefixed provider keys in free-text strings.
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{6,}")

#: Pattern masking DashScope / Aliyun-style API keys (32+ hex chars).
_DASHSCOPE_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SecretSerializationError(TypeError):
    """Raised when code attempts to JSON-serialize a :class:`Secret`.

    JSON serialization of secrets is always refused — even accidentally
    including a ``Secret`` in a Pydantic response model would expose it.
    """

    def __init__(self) -> None:
        super().__init__(
            "Secret values must not be JSON-serialized. "
            "Unwrap with .get_secret_value() only when absolutely necessary."
        )


# ---------------------------------------------------------------------------
# Secret wrapper
# ---------------------------------------------------------------------------


class Secret:
    """Opaque wrapper for a sensitive string value.

    * ``repr()`` and ``str()`` always return ``"[REDACTED]"``.
    * Equality comparison works on the *underlying* value so the wrapper is
      usable in tests without unwrapping.
    * ``__json__`` / custom JSON encoder hooks raise
      :exc:`SecretSerializationError`.
    * ``.get_secret_value()`` returns the raw string for the rare callers that
      legitimately need it (e.g. signing, hashing).

    >>> s = Secret("sk-12345")
    >>> repr(s)
    '[REDACTED]'
    >>> str(s)
    '[REDACTED]'
    >>> s.get_secret_value()
    'sk-12345'
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"Secret requires a str, got {type(value).__name__}")
        object.__setattr__(self, "_value", value)

    # ------------------------------------------------------------------
    # Safe representations — always masked.
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:  # noqa: D105
        return REDACTED

    # ------------------------------------------------------------------
    # Equality / hashing — operate on the underlying value.
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self.get_secret_value() == other.get_secret_value()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.get_secret_value())

    # ------------------------------------------------------------------
    # Mutation guard.
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Secret is immutable")

    # ------------------------------------------------------------------
    # JSON serialization guard.
    # ------------------------------------------------------------------

    def __json__(self) -> None:  # hook used by some custom encoders
        raise SecretSerializationError

    # ------------------------------------------------------------------
    # Pydantic v2 support: refuse JSON serialization.
    # ------------------------------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        """Pydantic v2 schema: accept str input, serialize as error."""
        from pydantic_core import core_schema  # local import — optional dep

        def _raise(_: Any) -> str:
            raise SecretSerializationError

        return core_schema.no_info_plain_validator_function(
            lambda v: cls(v) if isinstance(v, str) else cls(str(v)),
            serialization=core_schema.plain_serializer_function_ser_schema(
                _raise,
                info_arg=False,
            ),
        )

    # ------------------------------------------------------------------
    # Public accessor.
    # ------------------------------------------------------------------

    def get_secret_value(self) -> str:
        """Return the raw secret string.

        Call this only when you genuinely need the plaintext (e.g. to pass it
        to a cryptographic function).  Never pass the result to a logger.
        """
        return object.__getattribute__(self, "_value")


# ---------------------------------------------------------------------------
# Key sensitivity check
# ---------------------------------------------------------------------------


def is_sensitive_key(key: str) -> bool:
    """Return ``True`` when *key* looks like it holds a secret.

    Args:
        key: A log event key name.

    Returns:
        ``True`` if any of the following apply:

        * The lowercased key matches exactly one of the entries in
          :data:`_SENSITIVE_EXACT`.
        * The lowercased key *contains* one of the :data:`_SENSITIVE_SUBSTRINGS`
          as a substring.
    """
    lowered = key.lower()
    if lowered in _SENSITIVE_EXACT:
        return True
    return any(sub in lowered for sub in _SENSITIVE_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Free-text masking
# ---------------------------------------------------------------------------


def _mask_text(text: str) -> str:
    """Mask secret-looking patterns embedded in a free-text string.

    Patterns replaced:

    * ``Bearer <token>`` → ``Bearer [REDACTED]``
    * ``sk-<hex/b64>`` → ``[REDACTED]``
    * Long hex sequences (DashScope-style API keys) → ``[REDACTED]``
    """
    masked = _BEARER_RE.sub("Bearer " + REDACTED, text)
    masked = _SK_KEY_RE.sub(REDACTED, masked)
    masked = _DASHSCOPE_RE.sub(REDACTED, masked)
    return masked


def _redact_value(value: Any) -> Any:
    """Recursively redact sensitive-looking material in *value*."""
    if isinstance(value, Secret):
        return REDACTED
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_sensitive_key(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_value(item) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    if isinstance(value, str):
        return _mask_text(value)
    return value


# ---------------------------------------------------------------------------
# structlog processor
# ---------------------------------------------------------------------------


def redact_log_event(
    _logger: WrappedLogger,
    _name: str,
    event_dict: EventDict,
    *,
    extra_sensitive_keys: tuple[str, ...] = (),
) -> EventDict:
    """structlog processor: mask secrets before any renderer fires.

    Suitable for inclusion in a structlog processor chain:

    .. code-block:: python

        import structlog
        from app.sechardening.redaction import redact_log_event

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                redact_log_event,
                structlog.dev.ConsoleRenderer(),
            ]
        )

    Processing rules:

    * Keys whose names are sensitive (per :func:`is_sensitive_key` or
      *extra_sensitive_keys*) have their values replaced with
      :data:`REDACTED`.
    * All other values are recursively walked: nested dicts/lists are
      descended into; :class:`Secret` instances are replaced; string values
      are scanned for bearer tokens, ``sk-`` keys, and long hex sequences.

    Args:
        _logger: Unused (structlog convention).
        _name: Unused (structlog convention).
        event_dict: The mutable log event dictionary.
        extra_sensitive_keys: Additional lowercase key names (exact match)
            that should be treated as sensitive for this processor instance.

    Returns:
        The modified *event_dict* with secrets masked in-place.
    """
    extra_set: frozenset[str] = frozenset(k.lower() for k in extra_sensitive_keys)

    result: EventDict = {}
    for key, value in event_dict.items():
        key_lower = str(key).lower()
        if key_lower in extra_set or is_sensitive_key(str(key)):
            result[key] = REDACTED
        else:
            result[key] = _redact_value(value)
    return result


# ---------------------------------------------------------------------------
# Convenience: JSON-safe encoder that rejects Secret
# ---------------------------------------------------------------------------


class _SecretSafeEncoder(json.JSONEncoder):
    """JSON encoder that raises on :class:`Secret` rather than leaking it."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Secret):
            raise SecretSerializationError
        return super().default(obj)


def safe_json_dumps(obj: Any, **kwargs: Any) -> str:
    """``json.dumps`` wrapper that refuses to serialize :class:`Secret` values.

    Args:
        obj: The value to serialize.
        **kwargs: Forwarded to :func:`json.dumps`.

    Returns:
        A JSON string.

    Raises:
        SecretSerializationError: If *obj* or any nested value is a
            :class:`Secret`.
    """
    return json.dumps(obj, cls=_SecretSafeEncoder, **kwargs)
