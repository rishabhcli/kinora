"""The X.509-SVID certificate authority + trust bundle.

This is the crypto heart of the identity plane. It models a small, realistic PKI:

* :class:`CertificateAuthority` — a self-signed **root** or an **intermediate**
  signed by a parent. It mints short-lived leaf SVIDs whose only SAN is the
  workload's SPIFFE ID, marks them with the right key-usage/EKU bits for mTLS,
  and tracks revoked serials.
* :class:`TrustBundle` — the set of root certificates a verifier trusts, keyed by
  trust domain. Chain verification (in :mod:`app.zerotrust.identity.mtls`) walks a
  presented leaf+intermediates up to a bundle root.

Design choices that make this safe and deterministic:

* **Path-length constraints**: the root is ``CA:TRUE`` with ``pathlen`` set so an
  intermediate may sign leaves but not further sub-CAs by default.
* **Short-lived leaves**: default leaf TTL is one hour (SPIFFE's whole point is
  cheap, frequently-rotated credentials), and a clock seam decides ``notBefore``/
  ``notAfter`` so tests control validity windows exactly.
* **Deterministic serials available**: production uses random 128-bit serials;
  tests can pass an explicit ``serial`` for reproducible fixtures.
* **A small clock-skew backdate** on ``notBefore`` so a freshly-issued cert is not
  rejected by a peer whose clock is a few seconds behind.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.errors import (
    IssuanceError,
    UntrustedCertificateError,
)
from app.zerotrust.identity.keys import KeyAlgorithm, SigningKey
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain
from app.zerotrust.identity.svid import X509Svid

#: Default leaf SVID lifetime — short by design (SPIFFE rotates aggressively).
DEFAULT_LEAF_TTL = timedelta(hours=1)
#: Default CA certificate lifetime.
DEFAULT_CA_TTL = timedelta(days=3650)
#: Backdate ``notBefore`` slightly to tolerate peer clock skew.
_NOT_BEFORE_SKEW = timedelta(seconds=30)


def _hash_for(algorithm: KeyAlgorithm) -> hashes.SHA256 | None:
    # Ed25519 self-selects its hash; ECDSA needs SHA-256.
    return None if algorithm is KeyAlgorithm.ED25519 else hashes.SHA256()


def _random_serial() -> int:
    # 128-bit positive serial, per CA/Browser-Forum guidance.
    return x509.random_serial_number()


@dataclass(slots=True)
class CertificateAuthority:
    """A root or intermediate CA that signs X.509-SVIDs for one trust domain."""

    trust_domain: TrustDomain
    certificate: x509.Certificate
    signing_key: SigningKey
    parent: CertificateAuthority | None = None
    clock: Clock = field(default_factory=SystemClock)
    _revoked: set[int] = field(default_factory=set)

    # -- construction ------------------------------------------------------ #
    @classmethod
    def new_root(
        cls,
        trust_domain: str | TrustDomain,
        *,
        signing_key: SigningKey | None = None,
        algorithm: KeyAlgorithm = KeyAlgorithm.EC_P256,
        ttl: timedelta = DEFAULT_CA_TTL,
        clock: Clock | None = None,
        serial: int | None = None,
        path_length: int | None = 1,
    ) -> CertificateAuthority:
        """Create a self-signed root CA for *trust_domain*."""

        clock = clock or SystemClock()
        domain = (
            trust_domain
            if isinstance(trust_domain, TrustDomain)
            else TrustDomain(trust_domain)
        )
        key = signing_key or SigningKey.generate(algorithm)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Kinora"),
                x509.NameAttribute(NameOID.COMMON_NAME, f"Kinora Root CA ({domain.name})"),
            ]
        )
        not_before = clock.now() - _NOT_BEFORE_SKEW
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key().material)
            .serial_number(serial if serial is not None else _random_serial())
            .not_valid_before(not_before)
            .not_valid_after(clock.now() + ttl)
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=path_length), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.UniformResourceIdentifier(domain.id)]),
                critical=False,
            )
        )
        cert = builder.sign(key.material, _hash_for(key.algorithm))  # type: ignore[arg-type]
        return cls(domain, cert, key, parent=None, clock=clock)

    def new_intermediate(
        self,
        *,
        signing_key: SigningKey | None = None,
        algorithm: KeyAlgorithm | None = None,
        ttl: timedelta = DEFAULT_CA_TTL,
        serial: int | None = None,
        path_length: int | None = 0,
    ) -> CertificateAuthority:
        """Sign a new intermediate CA under this CA (same trust domain)."""

        algorithm = algorithm or self.signing_key.algorithm
        key = signing_key or SigningKey.generate(algorithm)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Kinora"),
                x509.NameAttribute(
                    NameOID.COMMON_NAME,
                    f"Kinora Intermediate CA ({self.trust_domain.name})",
                ),
            ]
        )
        not_before = self.clock.now() - _NOT_BEFORE_SKEW
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.certificate.subject)
            .public_key(key.public_key().material)
            .serial_number(serial if serial is not None else _random_serial())
            .not_valid_before(not_before)
            .not_valid_after(self.clock.now() + ttl)
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=path_length), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        cert = builder.sign(
            self.signing_key.material,  # type: ignore[arg-type]
            _hash_for(self.signing_key.algorithm),
        )
        return CertificateAuthority(
            self.trust_domain, cert, key, parent=self, clock=self.clock
        )

    # -- issuance ---------------------------------------------------------- #
    def issue_svid(
        self,
        spiffe_id: SpiffeId,
        *,
        workload_key: SigningKey | None = None,
        algorithm: KeyAlgorithm | None = None,
        ttl: timedelta = DEFAULT_LEAF_TTL,
        not_before: datetime | None = None,
        dns_sans: Iterable[str] = (),
        serial: int | None = None,
    ) -> X509Svid:
        """Mint a short-lived leaf X.509-SVID for *spiffe_id*.

        The leaf's *only* URI SAN is ``spiffe_id`` (the SPIFFE profile); it is an
        end-entity cert (``CA:FALSE``) marked for TLS client+server auth.
        """

        spiffe_id.require_domain(self.trust_domain)
        if spiffe_id.is_trust_domain:
            raise IssuanceError("cannot issue an SVID for a bare trust-domain id")
        algorithm = algorithm or self.signing_key.algorithm
        key = workload_key or SigningKey.generate(algorithm)
        issued_at = not_before or self.clock.now()
        nb = issued_at - _NOT_BEFORE_SKEW
        na = issued_at + ttl
        if na <= nb:
            raise IssuanceError("SVID TTL must be positive")
        san_values: list[x509.GeneralName] = [
            x509.UniformResourceIdentifier(spiffe_id.uri)
        ]
        san_values.extend(x509.DNSName(d) for d in dns_sans)
        builder = (
            x509.CertificateBuilder()
            # SPIFFE leaves carry an empty subject DN; identity is in the SAN.
            .subject_name(x509.Name([]))
            .issuer_name(self.certificate.subject)
            .public_key(key.public_key().material)
            .serial_number(serial if serial is not None else _random_serial())
            .not_valid_before(nb)
            .not_valid_after(na)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    key_agreement=algorithm is KeyAlgorithm.EC_P256,
                    content_commitment=False,
                    data_encipherment=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
                ),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(san_values),
                # SPIFFE: SAN is critical because the empty subject carries no name.
                critical=True,
            )
        )
        try:
            leaf = builder.sign(
                self.signing_key.material,  # type: ignore[arg-type]
                _hash_for(self.signing_key.algorithm),
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise IssuanceError(f"failed to sign SVID: {exc}") from exc
        return X509Svid(spiffe_id, leaf, self.intermediate_chain(), key)

    # -- chain / bundle ---------------------------------------------------- #
    def intermediate_chain(self) -> tuple[x509.Certificate, ...]:
        """The intermediate certs between an issued leaf and the root.

        Walks ``parent`` up to (but not including) the self-signed root.
        """

        chain: list[x509.Certificate] = []
        node: CertificateAuthority | None = self
        while node is not None and node.parent is not None:
            chain.append(node.certificate)
            node = node.parent
        return tuple(chain)

    def root(self) -> CertificateAuthority:
        """The self-signed root at the top of this CA's chain."""

        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def trust_bundle(self) -> TrustBundle:
        """A single-domain trust bundle anchored at this CA's root."""

        return TrustBundle().add(self.trust_domain, self.root().certificate)

    # -- revocation -------------------------------------------------------- #
    def revoke(self, serial: int) -> None:
        """Mark a serial revoked (consulted by the mTLS verifier)."""

        self._revoked.add(serial)

    def is_revoked(self, serial: int) -> bool:
        return serial in self._revoked

    @property
    def revoked_serials(self) -> frozenset[int]:
        return frozenset(self._revoked)


@dataclass(slots=True)
class TrustBundle:
    """The set of trusted root certificates, keyed by trust domain.

    A verifier consults the bundle to decide whether a presented chain anchors in
    a root it trusts. Supports multiple domains so a federated mesh can trust
    several roots at once.
    """

    _roots: dict[str, list[x509.Certificate]] = field(default_factory=dict)

    def add(self, domain: str | TrustDomain, root: x509.Certificate) -> TrustBundle:
        """Add a trusted root for *domain* (returns ``self`` for chaining)."""

        name = domain.name if isinstance(domain, TrustDomain) else TrustDomain(domain).name
        self._roots.setdefault(name, [])
        # de-dup by fingerprint so re-adding a root is idempotent
        fp = root.fingerprint(hashes.SHA256())
        if all(c.fingerprint(hashes.SHA256()) != fp for c in self._roots[name]):
            self._roots[name].append(root)
        return self

    def roots_for(self, domain: str | TrustDomain) -> tuple[x509.Certificate, ...]:
        name = domain.name if isinstance(domain, TrustDomain) else TrustDomain(domain).name
        return tuple(self._roots.get(name, ()))

    def domains(self) -> frozenset[str]:
        return frozenset(self._roots)

    def has_domain(self, domain: str | TrustDomain) -> bool:
        name = domain.name if isinstance(domain, TrustDomain) else TrustDomain(domain).name
        return name in self._roots and bool(self._roots[name])

    def require_domain(self, domain: str | TrustDomain) -> None:
        if not self.has_domain(domain):
            name = domain.name if isinstance(domain, TrustDomain) else domain
            raise UntrustedCertificateError(f"no trusted root for domain {name!r}")

    def merge(self, other: TrustBundle) -> TrustBundle:
        """Federate another bundle into this one."""

        for name, roots in other._roots.items():
            for root in roots:
                self.add(name, root)
        return self


__all__ = [
    "DEFAULT_CA_TTL",
    "DEFAULT_LEAF_TTL",
    "CertificateAuthority",
    "TrustBundle",
]
