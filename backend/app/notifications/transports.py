"""Pluggable channel transports — the wire that actually sends bytes.

A *channel* (email / push / webhook) is the policy; a *transport* is the
mechanism. Splitting them is what makes the platform testable: production wires
an SMTP / HTTP / push-provider transport, while tests inject the **fakes** here
(``InMemoryEmailTransport``, ``RecordingPushTransport``, ``RecordingWebhookTransport``)
so the whole dispatch → retry → circuit-breaker → dead-letter path runs with zero
network and zero credits (a hard constraint).

A transport's only contract is :meth:`send`, which returns a provider message id
on success and raises a :class:`~app.notifications.errors.TransportError` on
failure — ``retryable`` on the error drives the §12.1 retry decision. The fakes
can be told to fail a configurable number of times (transiently) or permanently,
which is exactly what the retry / circuit-breaker tests need.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.notifications.errors import (
    PermanentTransportError,
    TransientTransportError,
)
from app.notifications.models import RenderedMessage


@dataclass(frozen=True, slots=True)
class TransportResult:
    """The outcome of a successful send (the provider's id, for status tracking)."""

    provider_message_id: str


class EmailTransport(Protocol):
    """Sends a rendered message to an email address."""

    async def send(self, *, address: str, message: RenderedMessage) -> TransportResult: ...


class PushTransport(Protocol):
    """Sends a rendered message to a device push token."""

    async def send(self, *, token: str, message: RenderedMessage) -> TransportResult: ...


class WebhookTransport(Protocol):
    """POSTs a signed JSON body to a URL with the given headers."""

    async def send(
        self, *, url: str, body: bytes, headers: dict[str, str]
    ) -> TransportResult: ...


# --------------------------------------------------------------------------- #
# Fakes (tests / local dev) — no network, fully scriptable
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SentEmail:
    address: str
    message: RenderedMessage


@dataclass(slots=True)
class SentPush:
    token: str
    message: RenderedMessage


@dataclass(slots=True)
class SentWebhook:
    url: str
    body: bytes
    headers: dict[str, str]


class _FailureScript:
    """Shared helper: fail the first ``fail_times`` calls, then succeed.

    ``permanent`` makes those failures non-retryable (a 4xx), otherwise they are
    transient (a 5xx / network blip). ``always_fail`` never succeeds — used to
    drive a circuit fully open / a delivery to the dead-letter store.
    """

    def __init__(
        self, *, fail_times: int = 0, permanent: bool = False, always_fail: bool = False
    ) -> None:
        self.fail_times = fail_times
        self.permanent = permanent
        self.always_fail = always_fail
        self.calls = 0

    def check(self, target: str) -> None:
        self.calls += 1
        if self.always_fail or self.calls <= self.fail_times:
            error = f"scripted failure #{self.calls} for {target}"
            if self.permanent:
                raise PermanentTransportError(error)
            raise TransientTransportError(error)


class InMemoryEmailTransport:
    """A fake email transport recording every send (the test double)."""

    def __init__(
        self, *, fail_times: int = 0, permanent: bool = False, always_fail: bool = False
    ) -> None:
        self.sent: list[SentEmail] = []
        self._script = _FailureScript(
            fail_times=fail_times, permanent=permanent, always_fail=always_fail
        )

    async def send(self, *, address: str, message: RenderedMessage) -> TransportResult:
        self._script.check(address)
        self.sent.append(SentEmail(address=address, message=message))
        return TransportResult(provider_message_id=f"email-{uuid.uuid4().hex[:12]}")


class RecordingPushTransport:
    """A fake push transport recording every send."""

    def __init__(
        self, *, fail_times: int = 0, permanent: bool = False, always_fail: bool = False
    ) -> None:
        self.sent: list[SentPush] = []
        self._script = _FailureScript(
            fail_times=fail_times, permanent=permanent, always_fail=always_fail
        )

    async def send(self, *, token: str, message: RenderedMessage) -> TransportResult:
        self._script.check(token)
        self.sent.append(SentPush(token=token, message=message))
        return TransportResult(provider_message_id=f"push-{uuid.uuid4().hex[:12]}")


class RecordingWebhookTransport:
    """A fake webhook transport recording every POST (no real HTTP)."""

    def __init__(
        self,
        *,
        fail_times: int = 0,
        permanent: bool = False,
        always_fail: bool = False,
        on_send: Callable[[SentWebhook], Awaitable[None]] | None = None,
    ) -> None:
        self.sent: list[SentWebhook] = []
        self._script = _FailureScript(
            fail_times=fail_times, permanent=permanent, always_fail=always_fail
        )
        self._on_send = on_send

    async def send(
        self, *, url: str, body: bytes, headers: dict[str, str]
    ) -> TransportResult:
        self._script.check(url)
        record = SentWebhook(url=url, body=body, headers=dict(headers))
        self.sent.append(record)
        if self._on_send is not None:
            await self._on_send(record)
        return TransportResult(provider_message_id=f"hook-{uuid.uuid4().hex[:12]}")


# --------------------------------------------------------------------------- #
# A logging "real" default — safe everywhere (writes to the log, no network).
# Production swaps these for SMTP / FCM / an HTTP client; here they keep the
# platform runnable end-to-end without external services or credits.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LoggingEmailTransport:
    """Default email transport: logs the message instead of sending (no SMTP)."""

    log: Callable[..., None] = field(default=lambda *a, **k: None)

    async def send(self, *, address: str, message: RenderedMessage) -> TransportResult:
        self.log("notifications.email.logged", address=address, subject=message.subject)
        return TransportResult(provider_message_id=f"email-log-{uuid.uuid4().hex[:8]}")


@dataclass(slots=True)
class LoggingPushTransport:
    """Default push transport: logs the message instead of sending (no provider)."""

    log: Callable[..., None] = field(default=lambda *a, **k: None)

    async def send(self, *, token: str, message: RenderedMessage) -> TransportResult:
        self.log("notifications.push.logged", token=token, subject=message.subject)
        return TransportResult(provider_message_id=f"push-log-{uuid.uuid4().hex[:8]}")


__all__ = [
    "EmailTransport",
    "InMemoryEmailTransport",
    "LoggingEmailTransport",
    "LoggingPushTransport",
    "PushTransport",
    "RecordingPushTransport",
    "RecordingWebhookTransport",
    "SentEmail",
    "SentPush",
    "SentWebhook",
    "TransportResult",
    "WebhookTransport",
]
