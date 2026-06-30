"""Tunable configuration for the API-hardening layer.

:class:`HardeningConfig` is a plain frozen dataclass so the middleware and the
test app can be configured without depending on the full
:class:`app.core.config.Settings` object. :meth:`HardeningConfig.from_settings`
projects the relevant knobs off ``Settings`` (all additive, all defaulted) so a
deployment can tune the limiter / idempotency window via env without editing
code, while tests can build a tiny config inline.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class HardeningConfig:
    """Knobs for every hardening middleware (all opt-in at wiring time)."""

    # --- request-id / correlation-id ---
    request_id_header: str = "X-Request-ID"
    correlation_id_header: str = "X-Correlation-ID"
    #: Trust an inbound request-id header (echo it) vs always mint a fresh one.
    #: Trusting is convenient for distributed tracing but lets a client choose its
    #: own id; minting is safer for an internet-facing edge.
    trust_inbound_request_id: bool = True

    # --- request limits / validation ---
    #: Max accepted request body in bytes (413 when a Content-Length exceeds it or
    #: a streamed body grows past it). 0 disables the cap.
    max_body_bytes: int = 8 * 1024 * 1024
    #: Methods whose bodies are validated for content-type.
    body_methods: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})
    #: Allowed request content-types for body methods (prefix match on the type,
    #: e.g. ``application/json`` matches ``application/json; charset=utf-8``).
    #: Empty disables content-type enforcement.
    allowed_content_types: frozenset[str] = frozenset(
        {"application/json", "multipart/form-data", "application/x-www-form-urlencoded"}
    )
    #: Paths exempt from content-type enforcement (prefix match) — e.g. raw upload
    #: routes that accept ``application/pdf``/octet-stream.
    content_type_exempt_prefixes: tuple[str, ...] = ()
    #: Paths exempt from the global body-size cap (prefix match) — e.g. an upload
    #: route that streams + enforces its own (larger) cap. Defaults to the
    #: content-type exemptions when unset is impractical, so this is explicit.
    body_size_exempt_prefixes: tuple[str, ...] = ()

    # --- idempotency ---
    #: Header carrying the client's idempotency key.
    idempotency_header: str = "Idempotency-Key"
    #: How long a stored response stays replayable, in seconds.
    idempotency_ttl_s: int = 24 * 3600
    #: Methods eligible for idempotent replay.
    idempotent_methods: frozenset[str] = frozenset({"POST"})
    #: Max idempotency-key length accepted (reject absurd keys).
    idempotency_key_max_len: int = 255
    #: Path prefixes under which idempotency applies. Empty == all paths.
    idempotency_path_prefixes: tuple[str, ...] = ()

    # --- rate limiting (token bucket) ---
    rate_limit_enabled: bool = True
    #: Default bucket capacity (burst) and refill rate (tokens/sec) when no
    #: per-route rule matches.
    rate_limit_capacity: int = 120
    rate_limit_refill_per_s: float = 2.0
    #: Paths exempt from rate limiting (prefix match) — health/metrics probes.
    rate_limit_exempt_prefixes: tuple[str, ...] = ("/health", "/ready", "/metrics")
    #: Emit the IETF draft ``RateLimit-*`` headers on every limited response.
    rate_limit_emit_headers: bool = True

    # --- problem+json ---
    #: When true, the problem handler renders ``application/problem+json`` with the
    #: RFC-7807 fields. When false (the default for the live app), errors keep the
    #: legacy ``{"error": {...}}`` envelope — see :mod:`.problem`.
    problem_json_enabled: bool = False
    #: Base URI used to build the ``type`` URI of a problem from its code.
    problem_type_base: str = "https://kinora.dev/problems/"
    #: Echo the request-id into the problem body (``instance``/``request_id``).
    problem_include_request_id: bool = True
    #: Reveal exception detail in 500s (local/dev only). Mirrors ``Settings.is_local``.
    expose_internal_errors: bool = False

    #: Extra arbitrary metadata (forward-compat; never required).
    extra: dict[str, Any] = field(default_factory=dict)

    def replace(self, **changes: Any) -> HardeningConfig:
        """Return a copy with ``changes`` applied (frozen-dataclass convenience)."""
        return replace(self, **changes)

    @classmethod
    def from_settings(cls, settings: Any) -> HardeningConfig:
        """Project a :class:`HardeningConfig` off a ``Settings``-like object.

        Reads only attributes that exist (every knob is optional), so this never
        raises on a partial / stub settings object. ``expose_internal_errors``
        defaults to the ``is_local`` flag so production never leaks internals.
        """

        def opt(name: str, default: Any) -> Any:
            return getattr(settings, name, default)

        base = cls()
        return cls(
            request_id_header=opt("hardening_request_id_header", base.request_id_header),
            correlation_id_header=opt(
                "hardening_correlation_id_header", base.correlation_id_header
            ),
            trust_inbound_request_id=opt(
                "hardening_trust_inbound_request_id", base.trust_inbound_request_id
            ),
            max_body_bytes=int(opt("hardening_max_body_bytes", base.max_body_bytes)),
            idempotency_header=opt("hardening_idempotency_header", base.idempotency_header),
            idempotency_ttl_s=int(opt("hardening_idempotency_ttl_s", base.idempotency_ttl_s)),
            rate_limit_enabled=bool(opt("hardening_rate_limit_enabled", base.rate_limit_enabled)),
            rate_limit_capacity=int(
                opt("hardening_rate_limit_capacity", base.rate_limit_capacity)
            ),
            rate_limit_refill_per_s=float(
                opt("hardening_rate_limit_refill_per_s", base.rate_limit_refill_per_s)
            ),
            problem_json_enabled=bool(
                opt("hardening_problem_json_enabled", base.problem_json_enabled)
            ),
            problem_type_base=opt("hardening_problem_type_base", base.problem_type_base),
            expose_internal_errors=bool(getattr(settings, "is_local", base.expose_internal_errors)),
        )


__all__ = ["HardeningConfig"]
