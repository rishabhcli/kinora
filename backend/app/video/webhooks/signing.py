"""Per-provider inbound-callback authentication (signature + anti-replay).

A callback URL is **unauthenticated by design** — there is no bearer token,
because the provider is a machine that only holds a *shared signing secret*. The
signature *is* the authentication, so verifying it correctly is the single most
security-critical thing this subsystem does. We support the two schemes real
async media providers use:

* **HMAC over the raw body** (Wan/DashScope-style): ``HMAC(secret, body)`` hex,
  optionally with a ``sha256=`` prefix, compared constant-time. Optionally the
  signed material is ``"{timestamp}.{body}"`` (Stripe/MiniMax-style) so the
  timestamp is *covered* by the MAC and cannot be tampered independently.
* **Shared-secret token** (a fixed header value), for providers that only offer a
  static callback secret — still constant-time compared, still timestamp-guarded.

**Anti-replay:** when the provider sends a timestamp header we reject deliveries
whose timestamp is outside ``± tolerance`` of now (default 300s). This stops a
captured-and-replayed callback from being re-accepted long after the fact. The
gateway's idempotency layer stops *fast* duplicates; the timestamp window stops
*slow* replays — the two are complementary.

Everything here is pure (no I/O); the only injected dependency is a ``clock`` so
tolerance can be tested deterministically.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.video.webhooks.errors import ReplayError, SignatureError, UnknownProviderError

#: A monotonic-enough wall clock returning epoch seconds; injectable for tests.
Clock = Callable[[], float]


def _utc_now() -> float:
    return time.time()


@dataclass(frozen=True, slots=True)
class ProviderSigningConfig:
    """How one provider signs its callbacks + the anti-replay policy.

    Attributes:
        provider: the URL slug (``wan`` / ``minimax`` / ``dashscope``).
        secret: the shared signing secret (never logged).
        scheme: ``"hmac"`` (default) or ``"shared_secret"``.
        signature_header: header carrying the signature / token.
        algorithm: digest name for HMAC (``"sha256"`` / ``"sha1"``).
        signature_prefix: prefix the provider prepends to the hex digest.
        timestamp_header: header carrying the event epoch-seconds, if any. When
            set, anti-replay is enforced (and, if ``sign_timestamp``, the MAC
            covers ``"{ts}.{body}"``).
        sign_timestamp: include the timestamp in the signed material.
        tolerance_s: max allowed absolute skew between the signed timestamp and
            now, in seconds. ``0`` disables the window (still verifies the MAC).
    """

    provider: str
    secret: str
    scheme: str = "hmac"
    signature_header: str = "x-kinora-signature"
    algorithm: str = "sha256"
    signature_prefix: str = ""
    timestamp_header: str | None = "x-kinora-timestamp"
    sign_timestamp: bool = True
    tolerance_s: int = 300


@dataclass
class SignatureVerifier:
    """Verify inbound callbacks against per-provider :class:`ProviderSigningConfig`.

    A single instance holds every configured provider; the route resolves by the
    URL slug. Unknown providers raise :class:`UnknownProviderError` so the route
    can answer distinctly from a *known* provider with a bad signature.
    """

    clock: Clock = field(default=_utc_now)
    _configs: dict[str, ProviderSigningConfig] = field(default_factory=dict)

    def register(self, config: ProviderSigningConfig) -> SignatureVerifier:
        """Register (or replace) a provider's signing config; returns self."""
        self._configs[config.provider] = config
        return self

    def is_configured(self, provider: str) -> bool:
        """Whether a signing config exists for ``provider``."""
        return provider in self._configs

    def providers(self) -> list[str]:
        """The configured provider slugs (sorted, for stable logging)."""
        return sorted(self._configs)

    def config_for(self, provider: str) -> ProviderSigningConfig:
        """Return the config for ``provider`` or raise :class:`UnknownProviderError`."""
        cfg = self._configs.get(provider)
        if cfg is None:
            raise UnknownProviderError(f"no signing config registered for provider {provider!r}")
        return cfg

    def verify(self, provider: str, body: bytes, headers: Mapping[str, str]) -> None:
        """Authenticate one inbound callback; raise on any failure.

        Order matters: we verify the *signature* first (cheap, constant-time) and
        only then the timestamp window, so a forged request is rejected as a
        signature failure rather than leaking that its timestamp was acceptable.

        Raises:
            UnknownProviderError: no config for ``provider``.
            SignatureError: header missing or MAC/token mismatch.
            ReplayError: signed timestamp outside the tolerance window.
        """
        cfg = self.config_for(provider)
        lowered = {k.lower(): v for k, v in headers.items()}

        provided = lowered.get(cfg.signature_header.lower())
        if not provided:
            raise SignatureError(
                f"missing signature header {cfg.signature_header!r} for {provider}"
            )
        provided = provided.strip()

        timestamp = lowered.get(cfg.timestamp_header.lower()) if cfg.timestamp_header else None

        expected = self._expected_signature(cfg, body, timestamp)
        if not hmac.compare_digest(provided, expected):
            raise SignatureError(f"signature mismatch for {provider}")

        # Only after the MAC verifies do we enforce freshness. A signed timestamp
        # that is missing when one is required is itself a rejection.
        self._check_timestamp(cfg, timestamp)

    # -- internals ---------------------------------------------------------- #
    def _expected_signature(
        self, cfg: ProviderSigningConfig, body: bytes, timestamp: str | None
    ) -> str:
        if cfg.scheme == "shared_secret":
            # A static token scheme: the "signature" is just the secret itself.
            # Still constant-time compared (via the caller's compare_digest).
            return f"{cfg.signature_prefix}{cfg.secret}"
        if cfg.scheme != "hmac":  # defensive: configs come from our own builder
            raise SignatureError(f"unsupported signing scheme {cfg.scheme!r} for {cfg.provider}")
        signed = body
        if cfg.sign_timestamp and cfg.timestamp_header and timestamp is not None:
            signed = f"{timestamp}.".encode() + body
        digest = hmac.new(
            cfg.secret.encode("utf-8"), signed, getattr(hashlib, cfg.algorithm)
        ).hexdigest()
        return f"{cfg.signature_prefix}{digest}"

    def _check_timestamp(self, cfg: ProviderSigningConfig, timestamp: str | None) -> None:
        if not cfg.timestamp_header or cfg.tolerance_s <= 0:
            return
        if timestamp is None:
            raise ReplayError(
                f"missing timestamp header {cfg.timestamp_header!r} for {cfg.provider}"
            )
        ts = _parse_epoch(timestamp)
        if ts is None:
            raise ReplayError(f"unparseable timestamp {timestamp!r} for {cfg.provider}")
        skew = abs(self.clock() - ts)
        if skew > cfg.tolerance_s:
            raise ReplayError(
                f"timestamp outside ±{cfg.tolerance_s}s replay window for {cfg.provider} "
                f"(skew={int(skew)}s)"
            )


def _parse_epoch(value: str) -> float | None:
    """Parse a timestamp header as epoch seconds or an ISO-8601 instant."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        return None


def sign_body(cfg: ProviderSigningConfig, body: bytes, *, timestamp: str | None = None) -> str:
    """Produce the signature a provider with ``cfg`` would send for ``body``.

    Test/operator helper — lets a caller synthesise a valid signed callback (and
    is what the deterministic tests use). Never call this on the receive path.
    """
    return SignatureVerifier()._expected_signature(cfg, body, timestamp)  # noqa: SLF001


__all__ = [
    "Clock",
    "ProviderSigningConfig",
    "SignatureVerifier",
    "sign_body",
]
