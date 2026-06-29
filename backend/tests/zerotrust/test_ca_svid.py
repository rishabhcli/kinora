"""CA issuance, SVID structure, and trust-bundle tests (deterministic keys)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.zerotrust.identity import (
    CertificateAuthority,
    IssuanceError,
    KeyAlgorithm,
    ManualClock,
    SigningKey,
    SpiffeId,
    TrustDomainMismatchError,
    spiffe_id_of_cert,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def test_root_is_self_signed_ca(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    bc = root.certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is True
    assert root.certificate.issuer == root.certificate.subject
    assert root.parent is None
    assert root.root() is root


def test_intermediate_chains_to_root(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    inter = root.new_intermediate()
    assert inter.parent is root
    assert inter.root() is root
    assert inter.certificate.issuer == root.certificate.subject
    chain = inter.intermediate_chain()
    assert chain == (inter.certificate,)


def test_issued_leaf_has_single_uri_san_and_correct_eku(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    inter = root.new_intermediate()
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/render-worker")
    svid = inter.issue_svid(sid, ttl=timedelta(hours=1))

    assert spiffe_id_of_cert(svid.leaf) == sid
    assert svid.spiffe_id == sid
    bc = svid.leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is False
    eku = svid.leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku.value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku.value
    # leaf carries the intermediate in its presentation chain
    assert svid.intermediates == (inter.certificate,)
    assert svid.chain == (svid.leaf, inter.certificate)


def test_leaf_validity_window_tracks_clock_and_ttl(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/api")
    svid = root.issue_svid(sid, ttl=timedelta(hours=2))
    # notBefore is backdated by the skew, notAfter is now+ttl
    assert svid.not_before <= clock.now()
    assert svid.lifetime_seconds() == pytest.approx(2 * 3600 + 30, abs=1)
    assert svid.is_valid_at(clock.now())
    clock.advance(hours=3)
    assert not svid.is_valid_at(clock.now())


def test_cannot_issue_for_other_domain(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    with pytest.raises(TrustDomainMismatchError):
        root.issue_svid(SpiffeId.parse("spiffe://other.internal/x"))


def test_cannot_issue_for_bare_trust_domain(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    with pytest.raises(IssuanceError):
        root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}"))


def test_ed25519_ca_issues_verifiable_leaf(clock: ManualClock, ed_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(
        TRUST_DOMAIN, signing_key=ed_ca_key, algorithm=KeyAlgorithm.ED25519, clock=clock
    )
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/worker")
    svid = root.issue_svid(sid)
    assert spiffe_id_of_cert(svid.leaf) == sid


def test_deterministic_serial(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x")
    svid = root.issue_svid(sid, serial=4242)
    assert svid.serial_number == 4242


def test_revocation_state(clock: ManualClock, ec_ca_key: SigningKey) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), serial=7)
    assert not root.is_revoked(7)
    root.revoke(7)
    assert root.is_revoked(7)
    assert 7 in root.revoked_serials


def test_trust_bundle_dedups_and_knows_domain(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    bundle = root.trust_bundle()
    assert bundle.has_domain(TRUST_DOMAIN)
    assert not bundle.has_domain("other.internal")
    # adding the same root again is idempotent
    bundle.add(TRUST_DOMAIN, root.certificate)
    assert len(bundle.roots_for(TRUST_DOMAIN)) == 1


def test_chain_pem_is_leaf_then_intermediates(
    clock: ManualClock, ec_ca_key: SigningKey
) -> None:
    root = CertificateAuthority.new_root(TRUST_DOMAIN, signing_key=ec_ca_key, clock=clock)
    inter = root.new_intermediate()
    svid = inter.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"))
    pem = svid.chain_pem()
    assert pem.count(b"BEGIN CERTIFICATE") == 2
