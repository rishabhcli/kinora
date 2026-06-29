"""SVID value types — the credentials a workload presents to prove its identity.

An **SVID** (SPIFFE Verifiable Identity Document) binds a :class:`SpiffeId` to a
cryptographic credential. Two forms exist:

* :class:`X509Svid` — a leaf X.509 certificate (plus its intermediate chain and
  the private key) whose single URI SAN is the workload's SPIFFE ID. This is what
  the mTLS seam presents and verifies.
* :class:`JwtSvid` — a signed JWT whose ``sub`` is the SPIFFE ID and whose ``aud``
  names the intended audience(s). This is what a workload presents to a peer that
  speaks JWT rather than mTLS (an API gateway, a service mesh sidecar).

These are immutable carriers — issuance lives in :mod:`app.zerotrust.identity.ca`
/ :mod:`app.zerotrust.identity.jwt_svid`, verification in the mTLS / token seams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from app.zerotrust.identity.errors import CertificateError
from app.zerotrust.identity.keys import SigningKey
from app.zerotrust.identity.spiffe import SpiffeId


def spiffe_id_of_cert(cert: x509.Certificate) -> SpiffeId:
    """Extract the single SPIFFE URI SAN from *cert*.

    Per the SPIFFE X.509-SVID profile a leaf carries **exactly one** URI SAN and
    it is the SPIFFE ID; anything else is a malformed SVID.
    """

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound as exc:  # pragma: no cover - defensive
        raise CertificateError("certificate has no SubjectAlternativeName") from exc
    uris = san.value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise CertificateError(
            f"SVID must carry exactly one URI SAN, found {len(uris)}"
        )
    return SpiffeId.parse(uris[0])


@dataclass(frozen=True, slots=True)
class X509Svid:
    """An X.509-SVID: a leaf cert, its intermediate chain, and the private key."""

    spiffe_id: SpiffeId
    leaf: x509.Certificate
    intermediates: tuple[x509.Certificate, ...] = ()
    private_key: SigningKey | None = None

    @property
    def serial_number(self) -> int:
        """The leaf certificate serial (the revocation key)."""

        return self.leaf.serial_number

    @property
    def not_before(self) -> datetime:
        return self.leaf.not_valid_before_utc

    @property
    def not_after(self) -> datetime:
        return self.leaf.not_valid_after_utc

    @property
    def chain(self) -> tuple[x509.Certificate, ...]:
        """The full presentation chain: leaf first, then intermediates."""

        return (self.leaf, *self.intermediates)

    def lifetime_seconds(self) -> float:
        return (self.not_after - self.not_before).total_seconds()

    def is_valid_at(self, moment: datetime) -> bool:
        """Whether *moment* is within ``[notBefore, notAfter]``."""

        return self.not_before <= moment <= self.not_after

    def seconds_until_expiry(self, moment: datetime) -> float:
        return (self.not_after - moment).total_seconds()

    def chain_pem(self) -> bytes:
        """The leaf+intermediates chain in concatenated PEM (wire order)."""

        return b"".join(c.public_bytes(Encoding.PEM) for c in self.chain)

    def without_key(self) -> X509Svid:
        """A copy with the private key stripped — safe to hand to a verifier."""

        return X509Svid(self.spiffe_id, self.leaf, self.intermediates, None)


@dataclass(frozen=True, slots=True)
class JwtSvid:
    """A JWT-SVID: the compact-serialised token plus its decoded claims."""

    spiffe_id: SpiffeId
    audience: frozenset[str]
    token: str
    issued_at: datetime
    expires_at: datetime
    key_id: str
    claims: dict[str, object] = field(default_factory=dict)

    def is_valid_at(self, moment: datetime) -> bool:
        return self.issued_at <= moment <= self.expires_at

    def seconds_until_expiry(self, moment: datetime) -> float:
        return (self.expires_at - moment).total_seconds()


__all__ = ["JwtSvid", "X509Svid", "spiffe_id_of_cert"]
