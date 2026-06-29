"""The mTLS handshake / peer-verification seam.

Real mutual TLS lives in the transport layer; what an *application* needs is a
pure, testable answer to one question: *"given this peer's presented X.509-SVID
chain, is it a valid, trusted, unexpired credential, and what SPIFFE identity does
it prove?"* :class:`SvidVerifier` is that answer, and :func:`simulate_handshake`
exercises both directions (client proves to server, server proves to client) the
way a TLS 1.3 mutual handshake would — without a socket.

The verifier performs the checks a TLS stack performs on the certificate, in the
order that matters:

1. parse the SPIFFE ID out of the leaf's URI SAN (a malformed SVID fails here);
2. build the chain leaf → intermediates → a trusted root in the bundle, checking
   each issuer/subject link and signature (an untrusted chain fails here);
3. check ``notBefore``/``notAfter`` of **every** cert in the chain against the
   clock (an expired leaf or expired intermediate fails here);
4. check the leaf serial against the issuing CA's revocation set;
5. (optional) check the proven identity against an authorization predicate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from app.zerotrust.identity.ca import TrustBundle
from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.errors import (
    CertificateExpiredError,
    CertificateNotYetValidError,
    CertificateRevokedError,
    PeerVerificationError,
    UntrustedCertificateError,
)
from app.zerotrust.identity.spiffe import SpiffeId
from app.zerotrust.identity.svid import X509Svid, spiffe_id_of_cert


@dataclass(frozen=True, slots=True)
class VerifiedPeer:
    """The result of a successful peer verification."""

    spiffe_id: SpiffeId
    leaf: x509.Certificate
    verified_at: datetime

    @property
    def serial_number(self) -> int:
        return self.leaf.serial_number


def _verify_cert_signed_by(child: x509.Certificate, issuer: x509.Certificate) -> bool:
    """Verify *child* was signed by *issuer*'s key (and the names link).

    Supports the issuance suites this package mints (EC P-256, Ed25519) plus an
    RSA-PKCS#1v1.5 path so an externally-supplied (e.g. enterprise) root in the
    trust bundle still verifies. Any other key type is treated as unverifiable.
    """

    if child.issuer != issuer.subject:
        return False
    pub = issuer.public_key()
    sig_hash = child.signature_hash_algorithm
    try:
        if isinstance(pub, ec.EllipticCurvePublicKey):
            if sig_hash is None:
                return False
            pub.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(sig_hash))
        elif isinstance(pub, ed25519.Ed25519PublicKey):
            pub.verify(child.signature, child.tbs_certificate_bytes)
        elif isinstance(pub, rsa.RSAPublicKey):
            if sig_hash is None:
                return False
            pub.verify(
                child.signature,
                child.tbs_certificate_bytes,
                padding.PKCS1v15(),
                sig_hash,
            )
        else:
            return False
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def _fp(cert: x509.Certificate) -> bytes:
    return cert.fingerprint(hashes.SHA256())


@dataclass(slots=True)
class SvidVerifier:
    """Verifies presented X.509-SVID chains against a :class:`TrustBundle`."""

    trust_bundle: TrustBundle
    clock: Clock = field(default_factory=SystemClock)
    #: serials the verifier additionally treats as revoked (beyond CA state)
    revoked_serials: frozenset[int] = frozenset()

    def verify_chain(
        self,
        leaf: x509.Certificate,
        intermediates: tuple[x509.Certificate, ...] = (),
        *,
        at: datetime | None = None,
    ) -> VerifiedPeer:
        """Verify a leaf+intermediates chain; return the proven :class:`VerifiedPeer`."""

        now = at or self.clock.now()
        spiffe_id = spiffe_id_of_cert(leaf)
        self.trust_bundle.require_domain(spiffe_id.trust_domain)

        # 1. assemble a candidate chain ending at a trusted root.
        roots = self.trust_bundle.roots_for(spiffe_id.trust_domain)
        ordered = self._build_path(leaf, intermediates, roots)
        if ordered is None:
            raise UntrustedCertificateError(
                f"{spiffe_id.uri}: chain does not anchor in a trusted root"
            )

        # 2. validity window on every cert in the path.
        for cert in ordered:
            if now < cert.not_valid_before_utc:
                raise CertificateNotYetValidError(
                    f"certificate not yet valid (notBefore={cert.not_valid_before_utc})"
                )
            if now > cert.not_valid_after_utc:
                raise CertificateExpiredError(
                    f"certificate expired (notAfter={cert.not_valid_after_utc})"
                )

        # 3. revocation on the leaf serial.
        if leaf.serial_number in self.revoked_serials:
            raise CertificateRevokedError(f"leaf serial {leaf.serial_number} is revoked")

        return VerifiedPeer(spiffe_id, leaf, now)

    def verify_svid(self, svid: X509Svid, *, at: datetime | None = None) -> VerifiedPeer:
        """Verify a presented :class:`X509Svid` (ignores any attached key)."""

        return self.verify_chain(svid.leaf, svid.intermediates, at=at)

    def verify_peer(
        self,
        svid: X509Svid,
        *,
        at: datetime | None = None,
        authorize: Callable[[SpiffeId], bool] | None = None,
    ) -> VerifiedPeer:
        """Verify *svid* and, if given, apply an authorization predicate.

        Wraps any verification failure in :class:`PeerVerificationError` so the
        transport seam can treat *"this handshake failed"* uniformly while still
        carrying the precise cause as ``__cause__``.
        """

        try:
            peer = self.verify_svid(svid, at=at)
        except (
            UntrustedCertificateError,
            CertificateExpiredError,
            CertificateNotYetValidError,
            CertificateRevokedError,
        ) as exc:
            raise PeerVerificationError(str(exc)) from exc
        if authorize is not None and not authorize(peer.spiffe_id):
            raise PeerVerificationError(
                f"peer {peer.spiffe_id.uri} failed authorization predicate"
            )
        return peer

    # -- internals --------------------------------------------------------- #
    def _build_path(
        self,
        leaf: x509.Certificate,
        intermediates: tuple[x509.Certificate, ...],
        roots: tuple[x509.Certificate, ...],
    ) -> list[x509.Certificate] | None:
        """Return [leaf, ...intermediates, root] if a trusted path exists.

        Greedy issuer-walk: from the leaf, repeatedly find the cert (intermediate
        first, then trusted root) that signed the current node. Guards against
        loops with a visited set.
        """

        path: list[x509.Certificate] = [leaf]
        pool = list(intermediates)
        seen: set[bytes] = {_fp(leaf)}
        current = leaf
        for _ in range(len(intermediates) + 1):
            # a trusted root that signed current → done (don't append a self link)
            for root in roots:
                if _verify_cert_signed_by(current, root):
                    path.append(root)
                    return path
            # otherwise step up through an intermediate
            nxt: x509.Certificate | None = None
            for cand in pool:
                if _fp(cand) in seen:
                    continue
                if _verify_cert_signed_by(current, cand):
                    nxt = cand
                    break
            if nxt is None:
                return None
            path.append(nxt)
            seen.add(_fp(nxt))
            pool.remove(nxt)
            current = nxt
        return None


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    """The mutually-authenticated outcome of a simulated mTLS handshake."""

    client: VerifiedPeer
    server: VerifiedPeer


def simulate_handshake(
    *,
    client_svid: X509Svid,
    server_svid: X509Svid,
    client_verifier: SvidVerifier,
    server_verifier: SvidVerifier,
    at: datetime | None = None,
    client_authorize: Callable[[SpiffeId], bool] | None = None,
    server_authorize: Callable[[SpiffeId], bool] | None = None,
) -> HandshakeResult:
    """Run a full mutual handshake: each side verifies the other's SVID.

    * the **server** verifies the **client**'s presented SVID;
    * the **client** verifies the **server**'s presented SVID.

    Returns a :class:`HandshakeResult` carrying both proven identities, or raises
    :class:`PeerVerificationError` if either direction fails.
    """

    verified_client = server_verifier.verify_peer(
        client_svid, at=at, authorize=server_authorize
    )
    verified_server = client_verifier.verify_peer(
        server_svid, at=at, authorize=client_authorize
    )
    return HandshakeResult(client=verified_client, server=verified_server)


__all__ = [
    "HandshakeResult",
    "SvidVerifier",
    "VerifiedPeer",
    "simulate_handshake",
]
