"""The workload-identity issuance authority (the SPIFFE-server core).

:class:`IdentityIssuer` is where attestation, registration, and the CA meet. A
workload presents evidence; an attestor turns it into selectors; the registry
maps selectors to a :class:`RegistrationEntry`; the CA mints a short-lived
X.509-SVID for that entry's SPIFFE ID. The issuer also mints JWT-SVIDs for the
same identity, and exposes the :class:`TrustBundle` peers verify against.

This is the boundary other facets and the transport layer call. It is fully
deterministic given a fixed clock + fixed CA key: the only non-determinism is the
freshly-generated workload keypair (or a caller-supplied one).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from app.zerotrust.identity.attestation import (
    AttestationResult,
    StaticAttestor,
    WorkloadAttestor,
)
from app.zerotrust.identity.ca import CertificateAuthority, TrustBundle
from app.zerotrust.identity.clock import Clock
from app.zerotrust.identity.errors import AttestationError, IssuanceError
from app.zerotrust.identity.jwt_svid import (
    DEFAULT_JWT_TTL,
    JwtKeyRegistry,
    JwtSvidMinter,
)
from app.zerotrust.identity.keys import KeyAlgorithm, SigningKey
from app.zerotrust.identity.registry import RegistrationEntry, WorkloadRegistry
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain
from app.zerotrust.identity.svid import JwtSvid, X509Svid


@dataclass(slots=True)
class IssuedIdentity:
    """The bundle handed back to a freshly-attested workload."""

    entry: RegistrationEntry
    x509_svid: X509Svid
    attestation: AttestationResult

    @property
    def spiffe_id(self) -> SpiffeId:
        return self.entry.spiffe_id


@dataclass(slots=True)
class IdentityIssuer:
    """Issues X.509- and JWT-SVIDs for attested, registered workloads."""

    ca: CertificateAuthority
    registry: WorkloadRegistry
    jwt_minter: JwtSvidMinter

    @property
    def clock(self) -> Clock:
        return self.ca.clock

    @property
    def trust_domain(self) -> TrustDomain:
        return self.ca.trust_domain

    # -- construction helper ---------------------------------------------- #
    @classmethod
    def bootstrap(
        cls,
        trust_domain: str | TrustDomain,
        *,
        clock: Clock,
        ca_key: SigningKey | None = None,
        jwt_key: SigningKey | None = None,
        jwt_kid: str = "kinora-jwt-svid-1",
        algorithm: KeyAlgorithm = KeyAlgorithm.EC_P256,
        use_intermediate: bool = True,
    ) -> IdentityIssuer:
        """Stand up a fresh issuer: root (+ optional intermediate), registry, JWT minter."""

        domain = (
            trust_domain
            if isinstance(trust_domain, TrustDomain)
            else TrustDomain(trust_domain)
        )
        root = CertificateAuthority.new_root(
            domain, signing_key=ca_key, algorithm=algorithm, clock=clock
        )
        ca = root.new_intermediate() if use_intermediate else root
        registry = WorkloadRegistry(domain)
        minter = JwtSvidMinter(
            signing_key=jwt_key or SigningKey.generate(algorithm),
            key_id=jwt_kid,
            issuer=domain.id,
            clock=clock,
        )
        return cls(ca=ca, registry=registry, jwt_minter=minter)

    # -- issuance ---------------------------------------------------------- #
    def issue_for_attestation(
        self,
        attestation: AttestationResult,
        *,
        workload_key: SigningKey | None = None,
        ttl: timedelta | None = None,
    ) -> IssuedIdentity:
        """Attestation → registration match → minted X.509-SVID."""

        entry = self.registry.require_match(attestation)
        svid = self._mint_x509(entry, workload_key=workload_key, ttl=ttl)
        return IssuedIdentity(entry=entry, x509_svid=svid, attestation=attestation)

    def attest_and_issue(
        self,
        attestor: WorkloadAttestor,
        evidence: Mapping[str, str] | None = None,
        *,
        workload_key: SigningKey | None = None,
        ttl: timedelta | None = None,
    ) -> IssuedIdentity:
        """Run *attestor*, then issue for the result (the full server path)."""

        try:
            attestation = attestor.attest(evidence or {})
        except Exception as exc:  # pragma: no cover - attestor-defined
            raise AttestationError(f"attestation failed: {exc}") from exc
        return self.issue_for_attestation(
            attestation, workload_key=workload_key, ttl=ttl
        )

    def issue_for_id(
        self,
        spiffe_id: str | SpiffeId,
        *,
        workload_key: SigningKey | None = None,
        ttl: timedelta | None = None,
    ) -> X509Svid:
        """Issue directly for a registered SPIFFE ID (admin / pre-attested path)."""

        entry = self.registry.require_id(spiffe_id)
        return self._mint_x509(entry, workload_key=workload_key, ttl=ttl)

    def issue_jwt_for_attestation(
        self,
        attestation: AttestationResult,
        audience: str | Iterable[str],
        *,
        ttl: timedelta = DEFAULT_JWT_TTL,
    ) -> JwtSvid:
        """Mint a JWT-SVID for an attested workload, scoped to *audience*."""

        entry = self.registry.require_match(attestation)
        return self.jwt_minter.mint(entry.spiffe_id, audience, ttl=ttl)

    def issue_jwt_for_id(
        self,
        spiffe_id: str | SpiffeId,
        audience: str | Iterable[str],
        *,
        ttl: timedelta = DEFAULT_JWT_TTL,
    ) -> JwtSvid:
        entry = self.registry.require_id(spiffe_id)
        return self.jwt_minter.mint(entry.spiffe_id, audience, ttl=ttl)

    # -- trust material ---------------------------------------------------- #
    def trust_bundle(self) -> TrustBundle:
        """The X.509 trust bundle peers verify presented chains against."""

        return self.ca.trust_bundle()

    def jwt_registry(self) -> JwtKeyRegistry:
        """The JWT-SVID key registry verifiers resolve ``kid`` against."""

        return self.jwt_minter.registry()

    # -- internals --------------------------------------------------------- #
    def _mint_x509(
        self,
        entry: RegistrationEntry,
        *,
        workload_key: SigningKey | None,
        ttl: timedelta | None,
    ) -> X509Svid:
        try:
            return self.ca.issue_svid(
                entry.spiffe_id,
                workload_key=workload_key,
                ttl=ttl or entry.svid_ttl,
                dns_sans=entry.dns_sans,
            )
        except IssuanceError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise IssuanceError(f"failed to issue SVID for {entry.spiffe_id}: {exc}") from exc


__all__ = ["IdentityIssuer", "IssuedIdentity", "StaticAttestor"]
