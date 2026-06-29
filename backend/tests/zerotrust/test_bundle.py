"""SVID + trust-bundle serialization and federation tests."""

from __future__ import annotations

import pytest

from app.zerotrust.identity import (
    CertificateError,
    IdentityFabric,
    StaticAttestor,
    SvidVerifier,
    federate,
    svid_from_json,
    svid_from_pem,
    svid_to_json,
    svid_to_pem,
    trust_bundle_from_json,
    trust_bundle_to_json,
    trust_bundle_to_pem,
)


def _svid(fabric: IdentityFabric):  # type: ignore[no-untyped-def]
    fabric.register("spiffe://acme.kinora.internal/x", ["k8s:ns:x"])
    return fabric.issue(StaticAttestor.of("k8s:ns:x").attest({})).x509_svid


def test_svid_pem_round_trip(fabric: IdentityFabric) -> None:
    svid = _svid(fabric)
    chain, key = svid_to_pem(svid)
    assert chain.count(b"BEGIN CERTIFICATE") == 2  # leaf + 1 intermediate
    assert key is not None and b"PRIVATE KEY" in key
    restored = svid_from_pem(chain, key)
    assert restored.spiffe_id == svid.spiffe_id
    assert restored.serial_number == svid.serial_number
    assert restored.private_key is not None


def test_svid_pem_without_key(fabric: IdentityFabric) -> None:
    svid = _svid(fabric).without_key()
    chain, key = svid_to_pem(svid)
    assert key is None
    restored = svid_from_pem(chain)
    assert restored.private_key is None
    assert restored.spiffe_id == svid.spiffe_id


def test_svid_json_round_trip(fabric: IdentityFabric) -> None:
    svid = _svid(fabric)
    doc = svid_to_json(svid, include_key=True)
    restored = svid_from_json(doc)
    assert restored.spiffe_id == svid.spiffe_id
    assert restored.serial_number == svid.serial_number
    assert restored.intermediates[0].serial_number == svid.intermediates[0].serial_number


def test_svid_json_excludes_key_by_default(fabric: IdentityFabric) -> None:
    svid = _svid(fabric)
    restored = svid_from_json(svid_to_json(svid))
    assert restored.private_key is None


def test_cross_serialized_credential_verifies(fabric: IdentityFabric) -> None:
    """A PEM-shipped SVID verifies against a JSON-shipped bundle."""

    svid = _svid(fabric)
    chain, _ = svid_to_pem(svid)
    shipped_svid = svid_from_pem(chain)
    shipped_bundle = trust_bundle_from_json(trust_bundle_to_json(fabric.trust_bundle()))
    peer = SvidVerifier(shipped_bundle, clock=fabric.clock).verify_svid(shipped_svid)
    assert peer.spiffe_id == svid.spiffe_id


def test_malformed_pem_chain_rejected() -> None:
    with pytest.raises(CertificateError):
        svid_from_pem(b"not a cert")


def test_malformed_json_rejected() -> None:
    with pytest.raises(CertificateError):
        svid_from_json('{"bad": true}')


def test_trust_bundle_pem_export(fabric: IdentityFabric) -> None:
    pem = trust_bundle_to_pem(fabric.trust_bundle(), "acme.kinora.internal")
    assert pem.count(b"BEGIN CERTIFICATE") == 1
    with pytest.raises(CertificateError):
        trust_bundle_to_pem(fabric.trust_bundle(), "unknown.internal")


def test_federation_merges_domains(clock, ec_ca_key, ec_jwt_key) -> None:  # type: ignore[no-untyped-def]
    from app.zerotrust.identity import KeyAlgorithm

    a = IdentityFabric.bootstrap(
        "domain-a.internal", clock=clock, algorithm=KeyAlgorithm.EC_P256, ca_key=ec_ca_key
    )
    b = IdentityFabric.bootstrap("domain-b.internal", clock=clock)
    merged = federate(a.trust_bundle(), b.trust_bundle())
    assert merged.has_domain("domain-a.internal")
    assert merged.has_domain("domain-b.internal")

    # a workload from domain-b verifies against the federated bundle
    b.register("spiffe://domain-b.internal/w", ["k8s:ns:w"])
    svid = b.issue(StaticAttestor.of("k8s:ns:w").attest({})).x509_svid
    SvidVerifier(merged, clock=clock).verify_svid(svid)
