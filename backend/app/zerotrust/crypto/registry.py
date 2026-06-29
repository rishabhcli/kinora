"""Process-wide binding of the active :class:`CryptoProvider` for the ORM types.

A SQLAlchemy :class:`~sqlalchemy.types.TypeDecorator` is instantiated when the
*model class* is defined — long before any request, KMS, or provider exists — so
it cannot capture a provider in its constructor. The standard resolution is a
late-bound, process-wide accessor: the application sets the provider once at
startup (composition root / app lifespan), and every encrypted column resolves it
lazily on first bind/result.

A :class:`contextvars.ContextVar` is used rather than a plain global so a test (or
a multi-tenant request) can override the provider for the duration of a block
without leaking into siblings — the same pattern the rest of the backend uses for
request-scoped state.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

from app.zerotrust.crypto.context import CryptoProvider
from app.zerotrust.crypto.errors import CryptoConfigError

_PROVIDER: ContextVar[CryptoProvider | None] = ContextVar(
    "kinora_crypto_provider", default=None
)


def set_provider(provider: CryptoProvider) -> None:
    """Install the process-wide provider (call once at app startup)."""
    _PROVIDER.set(provider)


def get_provider() -> CryptoProvider:
    """Return the active provider, or raise if the app forgot to wire one.

    Raising (rather than silently storing plaintext) is the safe failure mode:
    an unconfigured encrypted column must never write cleartext to the database.
    """
    provider = _PROVIDER.get()
    if provider is None:
        raise CryptoConfigError(
            "no CryptoProvider is configured; call "
            "app.zerotrust.crypto.registry.set_provider() at startup before "
            "using an encrypted column"
        )
    return provider


@contextlib.contextmanager
def use_provider(provider: CryptoProvider) -> Iterator[CryptoProvider]:
    """Temporarily install ``provider`` (tests / scoped overrides)."""
    token = _PROVIDER.set(provider)
    try:
        yield provider
    finally:
        _PROVIDER.reset(token)


__all__ = ["get_provider", "set_provider", "use_provider"]
