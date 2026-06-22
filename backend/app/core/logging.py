"""Structured logging via structlog.

JSON output in non-local environments (machine-parseable for log pipelines),
pretty colorized console output locally. Never log secrets: pass only the data
you intend to surface — structlog renders exactly the key/values you bind.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import get_settings

#: Placeholder substituted for any redacted secret value.
REDACTED = "[REDACTED]"

#: Substrings that mark a key as carrying a secret. ``token`` is matched
#: *exactly* (below) so operational ids like ``cancel_token`` /
#: ``trajectory_token`` / ``provider_task_id`` are never needlessly scrubbed.
_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "bearer",
)

#: Patterns that mask secret-looking material embedded in free-text messages:
#: ``Bearer <jwt>`` authorization values and ``sk-...`` provider keys.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{6,}")


def _is_sensitive_key(key: str) -> bool:
    """True when a log key's name implies its value is a secret."""
    lowered = key.lower()
    if lowered == "token":
        return True
    return any(marker in lowered for marker in _SENSITIVE_KEY_SUBSTRINGS)


def _mask_text(text: str) -> str:
    """Mask bearer tokens / ``sk-`` keys embedded in a free-text string."""
    masked = _BEARER_RE.sub("Bearer " + REDACTED, text)
    return _SK_KEY_RE.sub(REDACTED, masked)


def _redact_value(value: Any) -> Any:
    """Recursively redact sensitive keys and mask secret-looking strings."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(str(key)) else _redact_value(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_value(item) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    if isinstance(value, str):
        return _mask_text(value)
    return value


def redact_secrets(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact secret values before rendering.

    Keys whose names look like secrets (``authorization``/``api_key``/
    ``dashscope_api_key``/``token``/``access_token``/``password``/``secret``)
    have their values replaced wholesale; remaining string values (including the
    log message) are scanned for bearer tokens and ``sk-`` keys and masked. The
    walk is recursive so nested dict/list payloads are covered too.
    """
    redacted: EventDict = {}
    for key, value in event_dict.items():
        if _is_sensitive_key(str(key)):
            redacted[key] = REDACTED
        else:
            redacted[key] = _redact_value(value)
    return redacted


def _build_processors(*, json_logs: bool) -> list[Processor]:
    """Assemble the structlog processor chain shared by every logger."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Redact secrets as the last step before rendering so nothing — message
        # or bound key/value — can leak an API key, bearer token, or password.
        redact_secrets,
    ]
    if json_logs:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    shared.append(renderer)
    return shared


def configure_logging(level: str = "INFO", *, json_logs: bool | None = None) -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        level: Minimum log level name (e.g. ``"INFO"``).
        json_logs: Force JSON (``True``) or console (``False``). When ``None``
            the format is derived from the environment (JSON outside ``local``).
    """
    settings = get_settings()
    if json_logs is None:
        json_logs = not settings.is_local

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    structlog.configure(
        processors=_build_processors(json_logs=json_logs),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Keep stdlib loggers (uvicorn, sqlalchemy, alembic) at the same threshold so
    # third-party output is consistent with our structured logs.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, optionally namespaced by ``name``."""
    return structlog.get_logger(name)
