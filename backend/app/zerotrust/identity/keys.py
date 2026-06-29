"""Asymmetric key material for SVID signing (CA chains + JWT-SVIDs).

A thin, typed wrapper over :mod:`cryptography`'s asymmetric primitives so the CA,
the JWT-SVID minter, and the trust bundle all speak one vocabulary regardless of
the underlying algorithm. Two signature suites are supported:

* **EC P-256** (``ES256``) — the default for X.509-SVIDs and JWT-SVIDs; small
  keys, fast, the SPIFFE-recommended default.
* **Ed25519** (``EdDSA``) — offered for callers who want a misuse-resistant
  signature with no curve/hash parameters to get wrong.

Determinism: key *generation* is not deterministic, so the test suite loads keys
from fixed PEM fixtures via :meth:`SigningKey.from_pem`. ECDSA signatures are
themselves randomised (per RFC 6979 we do **not** force deterministic-k here, so
two signatures over the same message differ) — tests therefore assert
**verification**, never signature byte-equality. Ed25519 signatures are
deterministic by construction, so those *can* be asserted byte-for-byte.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.types import (
    CertificatePublicKeyTypes,
    PrivateKeyTypes,
    PublicKeyTypes,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.zerotrust.identity.errors import CertificateError


class KeyAlgorithm(enum.StrEnum):
    """The supported asymmetric signature suites."""

    EC_P256 = "ec_p256"
    ED25519 = "ed25519"

    @property
    def jose_alg(self) -> str:
        """The JOSE ``alg`` header value for JWT-SVIDs signed with this key."""

        return {"ec_p256": "ES256", "ed25519": "EdDSA"}[self.value]


@dataclass(frozen=True, slots=True)
class PublicKey:
    """A public verification key, algorithm-tagged."""

    algorithm: KeyAlgorithm
    _key: CertificatePublicKeyTypes

    @property
    def material(self) -> CertificatePublicKeyTypes:
        """The underlying :mod:`cryptography` public-key object."""

        return self._key

    def to_pem(self) -> bytes:
        """SubjectPublicKeyInfo PEM encoding."""

        return self._key.public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo,
        )

    def to_der(self) -> bytes:
        """SubjectPublicKeyInfo DER encoding (the bytes a thumbprint hashes)."""

        return self._key.public_bytes(
            encoding=Encoding.DER,
            format=PublicFormat.SubjectPublicKeyInfo,
        )

    def verify(self, signature: bytes, message: bytes) -> bool:
        """Verify *signature* over *message*; ``False`` on any failure."""

        try:
            if self.algorithm is KeyAlgorithm.EC_P256:
                assert isinstance(self._key, ec.EllipticCurvePublicKey)
                self._key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            else:
                assert isinstance(self._key, ed25519.Ed25519PublicKey)
                self._key.verify(signature, message)
            return True
        except Exception:
            return False

    @classmethod
    def from_pem(cls, pem: bytes) -> PublicKey:
        loaded = serialization.load_pem_public_key(pem)
        return cls(_algorithm_of(loaded), _as_cert_public(loaded))

    @classmethod
    def _wrap(cls, key: PublicKeyTypes) -> PublicKey:
        return cls(_algorithm_of(key), _as_cert_public(key))


@dataclass(frozen=True, slots=True)
class SigningKey:
    """A private signing key, algorithm-tagged."""

    algorithm: KeyAlgorithm
    _key: PrivateKeyTypes

    @property
    def material(self) -> PrivateKeyTypes:
        """The underlying :mod:`cryptography` private-key object."""

        return self._key

    @classmethod
    def generate(cls, algorithm: KeyAlgorithm = KeyAlgorithm.EC_P256) -> SigningKey:
        """Generate a fresh signing key for *algorithm*."""

        if algorithm is KeyAlgorithm.EC_P256:
            return cls(algorithm, ec.generate_private_key(ec.SECP256R1()))
        return cls(algorithm, ed25519.Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes, password: bytes | None = None) -> SigningKey:
        """Load a PKCS#8 PEM private key (used by the deterministic fixtures)."""

        loaded = serialization.load_pem_private_key(pem, password=password)
        return cls(_algorithm_of(loaded), loaded)

    def public_key(self) -> PublicKey:
        """The matching :class:`PublicKey`."""

        return PublicKey(self.algorithm, _as_cert_public(self._key.public_key()))

    def sign(self, message: bytes) -> bytes:
        """Produce a raw signature over *message*."""

        if self.algorithm is KeyAlgorithm.EC_P256:
            assert isinstance(self._key, ec.EllipticCurvePrivateKey)
            return self._key.sign(message, ec.ECDSA(hashes.SHA256()))
        assert isinstance(self._key, ed25519.Ed25519PrivateKey)
        return self._key.sign(message)

    def to_pem(self) -> bytes:
        """PKCS#8 PEM encoding (unencrypted)."""

        return self._key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )


def _algorithm_of(key: object) -> KeyAlgorithm:
    if isinstance(key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)):
        if not isinstance(key.curve, ec.SECP256R1):
            raise CertificateError(f"unsupported EC curve: {key.curve.name}")
        return KeyAlgorithm.EC_P256
    if isinstance(key, (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey)):
        return KeyAlgorithm.ED25519
    raise CertificateError(f"unsupported key type: {type(key).__name__}")


def _as_cert_public(key: PublicKeyTypes) -> CertificatePublicKeyTypes:
    """Narrow an arbitrary public key to the cert-capable union we support."""

    if isinstance(key, (ec.EllipticCurvePublicKey, ed25519.Ed25519PublicKey)):
        return key
    raise CertificateError(f"unsupported public key type: {type(key).__name__}")


__all__ = ["KeyAlgorithm", "PublicKey", "SigningKey"]
