"""Structured logging via structlog.

JSON output in non-local environments (machine-parseable for log pipelines),
pretty colorized console output locally. Never log secrets: pass only the data
you intend to surface — structlog renders exactly the key/values you bind.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

from app.core.config import get_settings


def _build_processors(*, json_logs: bool) -> list[Processor]:
    """Assemble the structlog processor chain shared by every logger."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
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
