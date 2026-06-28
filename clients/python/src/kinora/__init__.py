"""Kinora — typed Python SDK for the Kinora API.

Kinora turns a book/PDF into a page-synced film that generates itself a few
seconds ahead of the reader. This SDK is a typed client over the FastAPI backend
(auth, books/upload, sessions + intent/seek, SSE event streaming, director
tools), with retries, typed errors, and sync + async clients.

    from kinora import KinoraClient

    with KinoraClient("http://localhost:8000") as client:
        client.auth.login("demo@kinora.local", "demo-password-123")
        for book in client.books.list():
            print(book.title, book.status)
"""

from __future__ import annotations

from . import errors, models
from ._transport import RetryPolicy
from .async_client import AsyncKinoraClient
from .client import KinoraClient
from .errors import (
    AuthError,
    BudgetExceededError,
    ConflictError,
    ForbiddenError,
    KinoraError,
    LiveVideoDisabledError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitError,
    ServerError,
    TimeoutError,
    UploadError,
    ValidationError,
)
from .events import Event, RawFrame, SseDecoder, decode_text_stream, parse_event
from .spec import (
    API_PREFIX,
    API_VERSION,
    CONFLICT_OPTIONS,
    DEFAULT_BASE_URL,
    ENDPOINTS,
    ERROR_TYPES,
    EVENTS,
)

__version__ = "1.0.0"

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "CONFLICT_OPTIONS",
    "DEFAULT_BASE_URL",
    "ENDPOINTS",
    "ERROR_TYPES",
    "EVENTS",
    "AsyncKinoraClient",
    "AuthError",
    "BudgetExceededError",
    "ConflictError",
    "Event",
    "ForbiddenError",
    "KinoraClient",
    "KinoraError",
    "LiveVideoDisabledError",
    "NetworkError",
    "NotFoundError",
    "ProviderError",
    "RateLimitError",
    "RawFrame",
    "RetryPolicy",
    "ServerError",
    "SseDecoder",
    "TimeoutError",
    "UploadError",
    "ValidationError",
    "__version__",
    "decode_text_stream",
    "errors",
    "models",
    "parse_event",
]
