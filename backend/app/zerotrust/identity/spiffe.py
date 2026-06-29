"""SPIFFE identity value types (the workload-identity naming layer).

A **SPIFFE ID** is the stable, URI-shaped name of a workload, independent of any
single credential it holds:

    spiffe://<trust-domain>/<workload-path>

* the **trust domain** (``acme.kinora.internal``) is the root of trust — every
  workload under it is vouched for by the same issuance authority;
* the **path** (``/render-worker``, ``/agents/critic``) names the workload within
  that domain.

This module is the pure naming/parsing core (no crypto, no I/O). It mirrors the
relevant rules of the SPIFFE-ID spec closely enough to be useful and safe:

* scheme is exactly ``spiffe`` (case-insensitive on input, normalised lower);
* the trust domain is a DNS-like label set, lower-cased, no userinfo/port;
* the path is percent-restricted to an unreserved + a couple of sub-delims set,
  must start with ``/`` when present, and may not contain empty (``//``) or dot
  segments — those are the segments that defeat path-prefix authz checks.

:class:`SpiffeId` is frozen + hashable so it can key registries, policy tables,
and trust bundles directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.zerotrust.identity.errors import (
    InvalidSpiffeIdError,
    TrustDomainMismatchError,
)

_SCHEME = "spiffe"
_MAX_ID_LENGTH = 2048  # SPIFFE spec: total ID must not exceed 2048 bytes.

# A trust domain is a set of DNS-like labels: lowercase letters, digits,
# hyphens, dots; no leading/trailing dot; each label 1-63 chars.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TRUST_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")

# Path segment chars per the SPIFFE spec restriction: unreserved characters
# plus a small, authz-safe sub-delims set. (No '%', so no percent-encoding
# ambiguity; no path-traversal characters beyond what we explicitly reject.)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_trust_domain(value: str) -> str:
    if not value:
        raise InvalidSpiffeIdError("trust domain must not be empty")
    lowered = value.lower()
    if lowered != value:
        # The spec mandates lower-case; we normalise rather than reject so a
        # caller passing 'ACME' gets a canonical id, but a value that *only*
        # differs by case is still considered the same domain.
        value = lowered
    if len(value) > 255:
        raise InvalidSpiffeIdError("trust domain too long")
    if not _TRUST_DOMAIN_RE.match(value):
        raise InvalidSpiffeIdError(f"invalid trust domain: {value!r}")
    return value


def _validate_path(value: str) -> str:
    """Validate + normalise a workload path, returning '' or '/seg/seg'."""

    if value in ("", "/"):
        return ""
    if not value.startswith("/"):
        raise InvalidSpiffeIdError("workload path must start with '/'")
    if value.endswith("/"):
        raise InvalidSpiffeIdError("workload path must not end with '/'")
    segments = value[1:].split("/")
    for seg in segments:
        if seg == "":
            raise InvalidSpiffeIdError("workload path must not contain empty segments")
        if seg in (".", ".."):
            raise InvalidSpiffeIdError("workload path must not contain dot segments")
        if not _SEGMENT_RE.match(seg):
            raise InvalidSpiffeIdError(f"invalid path segment: {seg!r}")
    return "/" + "/".join(segments)


@dataclass(frozen=True, slots=True)
class TrustDomain:
    """A normalised SPIFFE trust domain (the root of a trust fabric)."""

    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_trust_domain(self.name))

    @property
    def id(self) -> str:
        """The trust-domain SPIFFE ID (``spiffe://<name>``)."""

        return f"{_SCHEME}://{self.name}"

    def workload(self, path: str) -> SpiffeId:
        """Construct a :class:`SpiffeId` for *path* within this domain."""

        return SpiffeId(self.name, path)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class SpiffeId:
    """A fully-qualified SPIFFE workload identity.

    Construct from parts (``SpiffeId("acme.internal", "/render-worker")``) or
    parse a URI string with :meth:`parse`.
    """

    trust_domain: str
    path: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trust_domain", _validate_trust_domain(self.trust_domain))
        object.__setattr__(self, "path", _validate_path(self.path))
        if len(self.uri) > _MAX_ID_LENGTH:
            raise InvalidSpiffeIdError("SPIFFE ID exceeds 2048 bytes")

    # -- parsing ----------------------------------------------------------- #
    @classmethod
    def parse(cls, value: str) -> SpiffeId:
        """Parse a ``spiffe://...`` URI into a :class:`SpiffeId`."""

        if not value:
            raise InvalidSpiffeIdError("empty SPIFFE ID")
        if len(value) > _MAX_ID_LENGTH:
            raise InvalidSpiffeIdError("SPIFFE ID exceeds 2048 bytes")
        scheme_sep = "://"
        idx = value.find(scheme_sep)
        if idx == -1:
            raise InvalidSpiffeIdError("SPIFFE ID must be a spiffe:// URI")
        scheme = value[:idx]
        if scheme.lower() != _SCHEME:
            raise InvalidSpiffeIdError(f"unexpected scheme: {scheme!r}")
        rest = value[idx + len(scheme_sep) :]
        if "@" in rest.split("/", 1)[0]:
            raise InvalidSpiffeIdError("SPIFFE ID must not contain userinfo")
        slash = rest.find("/")
        if slash == -1:
            domain, path = rest, ""
        else:
            domain, path = rest[:slash], rest[slash:]
        if ":" in domain:
            raise InvalidSpiffeIdError("SPIFFE trust domain must not contain a port")
        return cls(domain, path)

    @classmethod
    def try_parse(cls, value: str) -> SpiffeId | None:
        """Like :meth:`parse` but returns ``None`` instead of raising."""

        try:
            return cls.parse(value)
        except InvalidSpiffeIdError:
            return None

    # -- accessors --------------------------------------------------------- #
    @property
    def uri(self) -> str:
        """The canonical ``spiffe://<domain><path>`` URI string."""

        return f"{_SCHEME}://{self.trust_domain}{self.path}"

    @property
    def domain(self) -> TrustDomain:
        """The :class:`TrustDomain` this identity belongs to."""

        return TrustDomain(self.trust_domain)

    @property
    def segments(self) -> tuple[str, ...]:
        """The workload-path segments (``('agents', 'critic')``)."""

        if not self.path:
            return ()
        return tuple(self.path[1:].split("/"))

    @property
    def is_trust_domain(self) -> bool:
        """True for a bare trust-domain id (``spiffe://acme.internal``)."""

        return self.path == ""

    # -- relationships ----------------------------------------------------- #
    def member_of(self, domain: str | TrustDomain) -> bool:
        """Whether this identity belongs to *domain*."""

        name = domain.name if isinstance(domain, TrustDomain) else _validate_trust_domain(domain)
        return self.trust_domain == name

    def is_under(self, prefix: SpiffeId) -> bool:
        """Whether this identity is at-or-below *prefix* in the same domain.

        Used by path-prefix authorization (``/agents`` authorizes
        ``/agents/critic``). Implemented on parsed segments — never on raw
        string ``startswith`` — so ``/agents`` does not match ``/agents-evil``.
        """

        if self.trust_domain != prefix.trust_domain:
            return False
        pseg = prefix.segments
        return self.segments[: len(pseg)] == pseg

    def require_domain(self, domain: str | TrustDomain) -> None:
        """Raise :class:`TrustDomainMismatchError` if not a member of *domain*."""

        if not self.member_of(domain):
            name = domain.name if isinstance(domain, TrustDomain) else domain
            raise TrustDomainMismatchError(
                f"{self.uri} is not a member of trust domain {name!r}"
            )

    def __str__(self) -> str:
        return self.uri


__all__ = ["SpiffeId", "TrustDomain"]
