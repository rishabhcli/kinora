"""Typed error hierarchy for the zero-trust identity + KMS plane.

A single root (:class:`ZeroTrustError`) lets callers ``except`` the whole plane,
while the leaf types let policy/transport code distinguish *"this peer is
unknown"* from *"this peer's certificate expired"* from *"this caller is not
authorized for that target"* — distinctions an mTLS handshake or an authorization
gate has to act on differently.
"""

from __future__ import annotations


class ZeroTrustError(Exception):
    """Base class for every error raised by :mod:`app.zerotrust.identity`."""


# --------------------------------------------------------------------------- #
# SPIFFE / identity
# --------------------------------------------------------------------------- #
class IdentityError(ZeroTrustError):
    """An identity could not be parsed, validated, or resolved."""


class InvalidSpiffeIdError(IdentityError):
    """A string is not a structurally valid SPIFFE ID."""


class TrustDomainMismatchError(IdentityError):
    """An identity belongs to a different trust domain than expected."""


class UnknownWorkloadError(IdentityError):
    """No registry entry exists for the referenced workload."""


class AttestationError(IdentityError):
    """A workload failed attestation (its claimed selectors were not proven)."""


# --------------------------------------------------------------------------- #
# CA / issuance
# --------------------------------------------------------------------------- #
class IssuanceError(ZeroTrustError):
    """An SVID could not be issued."""


class CertificateError(ZeroTrustError):
    """A certificate is malformed, untrusted, or otherwise unusable."""


class CertificateExpiredError(CertificateError):
    """A certificate is outside its validity window for the evaluated instant."""


class CertificateNotYetValidError(CertificateError):
    """A certificate's ``notBefore`` is in the future for the evaluated instant."""


class UntrustedCertificateError(CertificateError):
    """A certificate does not chain to any root in the trust bundle."""


class CertificateRevokedError(CertificateError):
    """A certificate's serial appears in the issuing CA's revocation set."""


# --------------------------------------------------------------------------- #
# mTLS handshake
# --------------------------------------------------------------------------- #
class HandshakeError(ZeroTrustError):
    """A simulated mTLS handshake/verification step failed."""


class PeerVerificationError(HandshakeError):
    """A peer's presented SVID failed verification against the trust bundle."""


# --------------------------------------------------------------------------- #
# JWT-SVID
# --------------------------------------------------------------------------- #
class TokenError(ZeroTrustError):
    """A JWT-SVID is malformed, expired, mis-audienced, or badly signed."""


class TokenExpiredError(TokenError):
    """A JWT-SVID is past its ``exp`` for the evaluated instant."""


class TokenAudienceError(TokenError):
    """A JWT-SVID's audience set does not include the required audience."""


class TokenSignatureError(TokenError):
    """A JWT-SVID's signature did not verify against the trust bundle."""


# --------------------------------------------------------------------------- #
# KMS / envelope encryption
# --------------------------------------------------------------------------- #
class KmsError(ZeroTrustError):
    """A key-management operation failed."""


class KeyNotFoundError(KmsError):
    """A referenced key (or key version) does not exist."""


class KeyDisabledError(KmsError):
    """A key/version exists but is disabled or scheduled for destruction."""


class DecryptionError(KmsError):
    """Ciphertext failed authenticated decryption (tampered or wrong key)."""


class KeyStateError(KmsError):
    """A key transition is illegal for the key's current lifecycle state."""


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
class SecretError(ZeroTrustError):
    """A secret-store operation failed."""


class SecretNotFoundError(SecretError):
    """No secret exists at the requested path (or version)."""


class LeaseError(SecretError):
    """A dynamic-secret lease operation failed."""


class LeaseExpiredError(LeaseError):
    """A lease is past its TTL / max-TTL and can no longer be renewed."""


class LeaseRevokedError(LeaseError):
    """A lease (or its parent secret) has been revoked."""


# --------------------------------------------------------------------------- #
# Policy / authorization
# --------------------------------------------------------------------------- #
class PolicyError(ZeroTrustError):
    """An authorization-policy operation failed."""


class AuthorizationError(PolicyError):
    """A caller workload is not authorized to call a target workload/action."""


__all__ = [
    "AttestationError",
    "AuthorizationError",
    "CertificateError",
    "CertificateExpiredError",
    "CertificateNotYetValidError",
    "CertificateRevokedError",
    "DecryptionError",
    "HandshakeError",
    "IdentityError",
    "InvalidSpiffeIdError",
    "IssuanceError",
    "KeyDisabledError",
    "KeyNotFoundError",
    "KeyStateError",
    "KmsError",
    "LeaseError",
    "LeaseExpiredError",
    "LeaseRevokedError",
    "PeerVerificationError",
    "PolicyError",
    "SecretError",
    "SecretNotFoundError",
    "TokenAudienceError",
    "TokenError",
    "TokenExpiredError",
    "TokenSignatureError",
    "TrustDomainMismatchError",
    "UnknownWorkloadError",
    "UntrustedCertificateError",
    "ZeroTrustError",
]
