"""Zero-trust **workload identity + key management** (facet A).

This package is the service-to-service trust substrate for Kinora — the
machine-identity counterpart of the end-user :mod:`app.auth` plane. It builds, on
stdlib + :mod:`cryptography` only and with an injectable clock for deterministic
tests, the five capabilities a zero-trust mesh needs of an identity authority:

1. **Workload identity** — SPIFFE IDs (:mod:`.spiffe`) and an issuance authority
   (:mod:`.issuer`) that turns workload *attestation* (:mod:`.attestation`) +
   *registration* (:mod:`.registry`) into short-lived X.509- and JWT-SVIDs.
2. **mTLS** — a CA chain (:mod:`.ca`), the SVID value types (:mod:`.svid`), and a
   pure handshake/verification seam (:mod:`.mtls`) that proves a peer's identity
   against a trust bundle.
3. **Rotation** — renew-before-expiry policy + a workload-side auto-rotating
   identity source (:mod:`.rotation`).
4. **KMS + envelope encryption** — a KMS abstraction with a DEK/KEK hierarchy,
   key versioning, rotation, and re-wrap (:mod:`.kms`); plus Vault-shaped secret
   storage with dynamic-secret leases (:mod:`.secrets`).
5. **Policy** — a default-deny *which-workload-may-call-which* engine
   (:mod:`.policy`).

:class:`IdentityFabric` (:mod:`.fabric`) wires all of the above against one trust
domain; :mod:`.contracts` names the Protocols sibling facets depend on.

No module here opens a socket on import; importing the package is side-effect
free.
"""

from __future__ import annotations

from app.zerotrust.identity.attestation import (
    AttestationResult,
    Selector,
    StaticAttestor,
    WorkloadAttestor,
    parse_selectors,
    selectors_satisfy,
)
from app.zerotrust.identity.bundle import (
    federate,
    svid_from_json,
    svid_from_pem,
    svid_to_json,
    svid_to_pem,
    trust_bundle_from_json,
    trust_bundle_to_json,
    trust_bundle_to_pem,
)
from app.zerotrust.identity.ca import (
    DEFAULT_CA_TTL,
    DEFAULT_LEAF_TTL,
    CertificateAuthority,
    TrustBundle,
)
from app.zerotrust.identity.clock import (
    Clock,
    FixedClock,
    ManualClock,
    SystemClock,
)
from app.zerotrust.identity.contracts import (
    AuthorizationGate,
    IdentityProvider,
    KeyManagementService,
    PeerVerifier,
    SecretProvider,
    TokenVerifier,
)
from app.zerotrust.identity.errors import (
    AttestationError,
    AuthorizationError,
    CertificateError,
    CertificateExpiredError,
    CertificateNotYetValidError,
    CertificateRevokedError,
    DecryptionError,
    HandshakeError,
    IdentityError,
    InvalidSpiffeIdError,
    IssuanceError,
    KeyDisabledError,
    KeyNotFoundError,
    KeyStateError,
    KmsError,
    LeaseError,
    LeaseExpiredError,
    LeaseRevokedError,
    PeerVerificationError,
    PolicyError,
    SecretError,
    SecretNotFoundError,
    TokenAudienceError,
    TokenError,
    TokenExpiredError,
    TokenSignatureError,
    TrustDomainMismatchError,
    UnknownWorkloadError,
    UntrustedCertificateError,
    ZeroTrustError,
)
from app.zerotrust.identity.fabric import DEFAULT_KEK_ID, IdentityFabric
from app.zerotrust.identity.issuer import IdentityIssuer, IssuedIdentity
from app.zerotrust.identity.jwt_svid import (
    JwtKeyRegistry,
    JwtSvidMinter,
    JwtSvidVerifier,
)
from app.zerotrust.identity.keys import KeyAlgorithm, PublicKey, SigningKey
from app.zerotrust.identity.kms import (
    DataKey,
    Envelope,
    EnvelopeCipher,
    KeyState,
    LocalKms,
    WrappedKey,
)
from app.zerotrust.identity.mtls import (
    HandshakeResult,
    SvidVerifier,
    VerifiedPeer,
    simulate_handshake,
)
from app.zerotrust.identity.policy import (
    AnyWorkload,
    AuthorizationPolicy,
    CallRequest,
    Decision,
    DomainMember,
    Effect,
    ExactId,
    Matcher,
    PathPrefix,
    PolicyRule,
    matcher_for,
)
from app.zerotrust.identity.registry import RegistrationEntry, WorkloadRegistry
from app.zerotrust.identity.rotation import (
    RotationEvent,
    RotationPolicy,
    WorkloadIdentitySource,
)
from app.zerotrust.identity.secrets import (
    DynamicSecretEngine,
    DynamicSecretRole,
    GeneratedSecret,
    Lease,
    SecretStore,
    SecretVersion,
)
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain
from app.zerotrust.identity.svid import JwtSvid, X509Svid, spiffe_id_of_cert

__all__ = [
    # spiffe
    "SpiffeId",
    "TrustDomain",
    # keys
    "KeyAlgorithm",
    "PublicKey",
    "SigningKey",
    # ca / svid
    "CertificateAuthority",
    "TrustBundle",
    "DEFAULT_CA_TTL",
    "DEFAULT_LEAF_TTL",
    "X509Svid",
    "JwtSvid",
    "spiffe_id_of_cert",
    # bundle / serialization / federation
    "svid_to_pem",
    "svid_from_pem",
    "svid_to_json",
    "svid_from_json",
    "trust_bundle_to_pem",
    "trust_bundle_to_json",
    "trust_bundle_from_json",
    "federate",
    # attestation / registry
    "AttestationResult",
    "Selector",
    "StaticAttestor",
    "WorkloadAttestor",
    "parse_selectors",
    "selectors_satisfy",
    "RegistrationEntry",
    "WorkloadRegistry",
    # issuer
    "IdentityIssuer",
    "IssuedIdentity",
    # mtls
    "SvidVerifier",
    "VerifiedPeer",
    "HandshakeResult",
    "simulate_handshake",
    # jwt-svid
    "JwtKeyRegistry",
    "JwtSvidMinter",
    "JwtSvidVerifier",
    # rotation
    "RotationEvent",
    "RotationPolicy",
    "WorkloadIdentitySource",
    # kms
    "DataKey",
    "Envelope",
    "EnvelopeCipher",
    "KeyState",
    "LocalKms",
    "WrappedKey",
    # secrets
    "DynamicSecretEngine",
    "DynamicSecretRole",
    "GeneratedSecret",
    "Lease",
    "SecretStore",
    "SecretVersion",
    # policy
    "AnyWorkload",
    "AuthorizationPolicy",
    "CallRequest",
    "Decision",
    "DomainMember",
    "Effect",
    "ExactId",
    "Matcher",
    "PathPrefix",
    "PolicyRule",
    "matcher_for",
    # fabric / contracts
    "DEFAULT_KEK_ID",
    "IdentityFabric",
    "AuthorizationGate",
    "IdentityProvider",
    "KeyManagementService",
    "PeerVerifier",
    "SecretProvider",
    "TokenVerifier",
    # clock
    "Clock",
    "FixedClock",
    "ManualClock",
    "SystemClock",
    # errors
    "ZeroTrustError",
    "IdentityError",
    "InvalidSpiffeIdError",
    "TrustDomainMismatchError",
    "UnknownWorkloadError",
    "AttestationError",
    "IssuanceError",
    "CertificateError",
    "CertificateExpiredError",
    "CertificateNotYetValidError",
    "CertificateRevokedError",
    "UntrustedCertificateError",
    "HandshakeError",
    "PeerVerificationError",
    "TokenError",
    "TokenExpiredError",
    "TokenAudienceError",
    "TokenSignatureError",
    "KmsError",
    "KeyNotFoundError",
    "KeyDisabledError",
    "KeyStateError",
    "DecryptionError",
    "SecretError",
    "SecretNotFoundError",
    "LeaseError",
    "LeaseExpiredError",
    "LeaseRevokedError",
    "PolicyError",
    "AuthorizationError",
]
