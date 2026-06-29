"""The identity/KMS contracts the sibling zero-trust facets consume.

Facet A (this package) owns the *implementations*; sibling facets (mesh / policy
enforcement, audit, secrets-consumers) depend only on these :class:`Protocol`s so
they can be wired against a fake in tests and the real services in production.
Re-exporting them from one module gives the siblings a single, stable import:

    from app.zerotrust.identity.contracts import (
        IdentityProvider, PeerVerifier, KeyManagementService,
        SecretProvider, AuthorizationGate,
    )

Nothing here opens a socket or pulls heavy deps; the concrete classes live in
their own modules and satisfy these protocols structurally.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from app.zerotrust.identity.attestation import AttestationResult
from app.zerotrust.identity.ca import TrustBundle
from app.zerotrust.identity.kms import (  # re-export
    DataKey,
    KeyManagementService,
    WrappedKey,
)
from app.zerotrust.identity.mtls import VerifiedPeer
from app.zerotrust.identity.policy import CallRequest, Decision
from app.zerotrust.identity.spiffe import SpiffeId
from app.zerotrust.identity.svid import JwtSvid, X509Svid


@runtime_checkable
class IdentityProvider(Protocol):
    """Mints workload SVIDs and exposes the trust material peers verify against.

    Satisfied by :class:`app.zerotrust.identity.issuer.IdentityIssuer`.
    """

    def issue_for_attestation(
        self, attestation: AttestationResult, *, ttl: timedelta | None = ...
    ) -> object: ...  # IssuedIdentity (avoid import cycle in the protocol)

    def issue_for_id(self, spiffe_id: str | SpiffeId) -> X509Svid: ...

    def issue_jwt_for_id(
        self, spiffe_id: str | SpiffeId, audience: str
    ) -> JwtSvid: ...

    def trust_bundle(self) -> TrustBundle: ...


@runtime_checkable
class PeerVerifier(Protocol):
    """Verifies a presented credential and returns the proven identity.

    Satisfied by :class:`app.zerotrust.identity.mtls.SvidVerifier`.
    """

    def verify_svid(self, svid: X509Svid) -> VerifiedPeer: ...


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a JWT-SVID token string.

    Satisfied by :class:`app.zerotrust.identity.jwt_svid.JwtSvidVerifier`.
    """

    def verify(self, token: str, *, audience: str | None = ...) -> JwtSvid: ...


@runtime_checkable
class SecretProvider(Protocol):
    """Reads static secrets at a path.

    Satisfied by :class:`app.zerotrust.identity.secrets.SecretStore`.
    """

    def get(self, path: str, *, version: int | None = ...) -> bytes: ...

    def get_map(self, path: str, *, version: int | None = ...) -> dict[str, str]: ...


@runtime_checkable
class AuthorizationGate(Protocol):
    """Decides whether a caller may invoke a target.

    Satisfied by :class:`app.zerotrust.identity.policy.AuthorizationPolicy`.
    """

    def evaluate(self, req: CallRequest) -> Decision: ...

    def is_allowed(self, req: CallRequest) -> bool: ...


__all__ = [
    "AuthorizationGate",
    "DataKey",
    "IdentityProvider",
    "KeyManagementService",
    "PeerVerifier",
    "SecretProvider",
    "TokenVerifier",
    "WrappedKey",
]
