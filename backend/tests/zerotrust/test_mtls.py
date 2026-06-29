"""mTLS verification + simulated mutual-handshake tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    CertificateAuthority,
    CertificateExpiredError,
    CertificateRevokedError,
    KeyAlgorithm,
    ManualClock,
    PeerVerificationError,
    SigningKey,
    SpiffeId,
    SvidVerifier,
    UntrustedCertificateError,
    simulate_handshake,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def _ca(clock: ManualClock, key: SigningKey) -> CertificateAuthority:
    return CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=key, clock=clock)


def test_verify_valid_leaf_through_intermediate(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = _ca(clock, ec_ca_key)
    inter = root.new_intermediate()
    svid = inter.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/render-worker"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    peer = verifier.verify_svid(svid)
    assert peer.spiffe_id.uri == f"spiffe://{TRUST_DOMAIN}/render-worker"
    assert peer.serial_number == svid.serial_number


def test_verify_direct_leaf_no_intermediate(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    svid = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/api"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    assert verifier.verify_svid(svid).spiffe_id.path == "/api"


def test_untrusted_root_is_rejected(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    real = _ca(clock, ec_ca_key)
    rogue = CertificateAuthority.new_root(TRUST_DOMAIN, clock=clock)  # different key
    rogue_svid = rogue.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/evil"))
    verifier = SvidVerifier(real.trust_bundle(), clock=clock)
    with pytest.raises(UntrustedCertificateError):
        verifier.verify_svid(rogue_svid)


def test_unknown_trust_domain_is_rejected(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    other = CertificateAuthority.new_root("other.internal", clock=clock)
    other_svid = other.issue_svid(SpiffeId.parse("spiffe://other.internal/x"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    with pytest.raises(UntrustedCertificateError):
        verifier.verify_svid(other_svid)


def test_expired_leaf_is_rejected(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    svid = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), ttl=timedelta(hours=1))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    verifier.verify_svid(svid)  # valid now
    clock.advance(hours=2)
    with pytest.raises(CertificateExpiredError):
        verifier.verify_svid(svid)


def test_revoked_serial_is_rejected(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    svid = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), serial=99)
    verifier = SvidVerifier(root.trust_bundle(), clock=clock, revoked_serials=frozenset({99}))
    with pytest.raises(CertificateRevokedError):
        verifier.verify_svid(svid)


def test_verify_peer_wraps_failures(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    rogue = CertificateAuthority.new_root(TRUST_DOMAIN, clock=clock)
    rogue_svid = rogue.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/evil"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    with pytest.raises(PeerVerificationError):
        verifier.verify_peer(rogue_svid)


def test_verify_peer_authorize_predicate(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = _ca(clock, ec_ca_key)
    svid = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    with pytest.raises(PeerVerificationError):
        verifier.verify_peer(svid, authorize=lambda sid: sid.path == "/allowed")
    peer = verifier.verify_peer(svid, authorize=lambda sid: sid.path == "/x")
    assert peer.spiffe_id.path == "/x"


def test_mutual_handshake_proves_both_identities(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = _ca(clock, ec_ca_key)
    client = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/client"))
    server = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/server"))
    bundle = root.trust_bundle()
    res = simulate_handshake(
        client_svid=client,
        server_svid=server,
        client_verifier=SvidVerifier(bundle, clock=clock),
        server_verifier=SvidVerifier(bundle, clock=clock),
    )
    assert res.client.spiffe_id.path == "/client"
    assert res.server.spiffe_id.path == "/server"


def test_mutual_handshake_fails_if_client_untrusted(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = _ca(clock, ec_ca_key)
    rogue = CertificateAuthority.new_root(TRUST_DOMAIN, clock=clock)
    client = rogue.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/client"))
    server = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/server"))
    bundle = root.trust_bundle()
    with pytest.raises(PeerVerificationError):
        simulate_handshake(
            client_svid=client,
            server_svid=server,
            client_verifier=SvidVerifier(bundle, clock=clock),
            server_verifier=SvidVerifier(bundle, clock=clock),
        )


def test_ed25519_chain_verifies(clock: ManualClock, ed_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(
        TRUST_DOMAIN, signing_key=ed_ca_key, algorithm=KeyAlgorithm.ED25519, clock=clock
    )
    inter = root.new_intermediate()
    svid = inter.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/worker"))
    verifier = SvidVerifier(root.trust_bundle(), clock=clock)
    assert verifier.verify_svid(svid).spiffe_id.path == "/worker"
