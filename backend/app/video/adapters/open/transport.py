"""HTTP transport for open / gateway adapters behind a default-OFF network flag.

Open-model and gateway providers (Replicate, fal.ai, a self-hosted ComfyUI box,
any OpenAPI endpoint) speak plain authenticated JSON-over-HTTP. Rather than each
adapter re-implementing retries / timeouts / breaker / usage accounting, they all
talk through :class:`OpenHttpTransport`, a thin wrapper over the project's
resilient :class:`app.providers.base.ProviderClient`.

Two safety gates layer on top of the shared client:

* **The network gate (``allow_network``) is OFF by default.** No adapter may touch
  the wire unless it is explicitly turned on (mirrors ``KINORA_LIVE_VIDEO`` for
  spend). Tests inject a mocked transport *and* flip the gate on, so the suite is
  hermetic and the production default never reaches the network by accident.
* **The spend gate (``KINORA_LIVE_VIDEO``) is sacred** and enforced one level up
  in :class:`~.base.BaseOpenAdapter` (so it is honoured even for self-hosted /
  free endpoints, which still consume compute).

The transport never reads env directly; everything comes from an injected
:class:`OpenTransportConfig`, so it is deterministic and exhaustively testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.errors import ProviderBadRequest

logger = get_logger("app.video.adapters.open.transport")

__all__ = ["NetworkDisabled", "OpenHttpTransport", "OpenTransportConfig"]


class NetworkDisabled(ProviderBadRequest):  # noqa: N818 - public name in contract
    """Raised when an open adapter would hit the wire with the network gate OFF.

    Non-retryable: re-attempting with the gate still off fails identically. This is
    a deliberate safety stop, not a transport fault — distinct from the spend gate
    (``LiveVideoDisabled``), which the base adapter raises *before* this one.
    """


@dataclass(frozen=True, slots=True)
class OpenTransportConfig:
    """Connection settings for one open / gateway endpoint (no env reads).

    Attributes:
        base_url: The endpoint root (e.g. ``https://api.replicate.com/v1``).
        api_key: Bearer token; ``None`` for an unauthenticated self-hosted box.
        auth_scheme: ``"bearer"`` (``Authorization: Bearer``), ``"token"``
            (``Authorization: Token`` — Replicate's classic scheme), ``"key"``
            (``Authorization: Key`` — fal.ai), or ``"none"``.
        allow_network: Master OFF-by-default switch; must be ``True`` to transmit.
        extra_headers: Static headers merged into every request (e.g. a
            ``Prefer: wait`` hint or a self-hosted API-version pin).
        timeout_s: Per-request timeout override.
    """

    base_url: str
    api_key: str | None = None
    auth_scheme: str = "bearer"
    allow_network: bool = False
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 60.0


class OpenHttpTransport:
    """Resilient JSON/byte transport for an open or gateway video endpoint.

    Wraps a :class:`ProviderClient` configured for the target host. Honours the
    OFF-by-default network gate on every call, normalizes auth header shape across
    Replicate / fal / bearer / unauthenticated schemes, and exposes the few verbs
    the adapters need (``post_json`` / ``get_json`` / ``download``).
    """

    def __init__(
        self,
        config: OpenTransportConfig,
        *,
        client: ProviderClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resilience: ResilienceConfig | None = None,
        settings: Any | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        if client is None:
            client = ProviderClient(
                settings=settings,
                transport=transport,
                resilience=resilience,
                base_url_override=config.base_url,
                # The shared client adds ``Authorization: Bearer`` itself; for the
                # other schemes we suppress that by passing an empty key and adding
                # the correct header in ``_headers``.
                api_key_override="" if config.auth_scheme != "bearer" else config.api_key,
            )
        self._client = client

    @property
    def config(self) -> OpenTransportConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _guard_network(self, op: str) -> None:
        if not self._config.allow_network:
            raise NetworkDisabled(
                f"open-video network is disabled (allow_network is off); {op} not sent",
            )

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build provider auth headers for the configured scheme.

        The shared client already injects ``Authorization: Bearer <key>`` for the
        ``bearer`` scheme, so we only add an explicit header for ``token`` / ``key``
        (and never duplicate the bearer one).
        """
        headers: dict[str, str] = dict(self._config.extra_headers)
        scheme = self._config.auth_scheme
        if scheme in ("token", "key"):
            prefix = "Token" if scheme == "token" else "Key"
            # Always set Authorization for these schemes — this also overwrites the
            # ``Authorization: Bearer <settings key>`` the shared client injects, so
            # no DashScope key ever leaks to a non-DashScope endpoint.
            headers["Authorization"] = f"{prefix} {self._config.api_key or ''}"
        elif scheme == "none":
            # No credential at all: overwrite the shared client's bearer with an
            # empty value so nothing leaks (httpx drops an empty-string header).
            headers["Authorization"] = ""
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def post_json(
        self,
        path: str,
        *,
        op: str,
        model: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST JSON to ``path`` (gated); return the parsed object."""
        self._guard_network(op)
        return await self._client.request_json(
            "POST",
            self._url(path),
            op=op,
            model=model,
            json=body,
            headers=self._headers(headers),
            timeout=self._config.timeout_s,
        )

    async def get_json(
        self,
        path: str,
        *,
        op: str,
        model: str,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET ``path`` as JSON (gated)."""
        self._guard_network(op)
        return await self._client.request_json(
            "GET",
            self._url(path),
            op=op,
            model=model,
            headers=self._headers(headers),
            timeout=self._config.timeout_s,
        )

    async def download(self, url: str, *, op: str = "video") -> bytes:
        """Download raw bytes from a (typically signed, expiring) result URL (gated)."""
        self._guard_network(op)
        return await self._client.download(url, op=op, timeout=self._config.timeout_s)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
