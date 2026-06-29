"""Issuance authority + SVID rotation correctness (deterministic clock)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    CertificateAuthority,
    IdentityIssuer,
    KeyAlgorithm,
    ManualClock,
    RotationEvent,
    RotationPolicy,
    SigningKey,
    SpiffeId,
    StaticAttestor,
    SvidVerifier,
    UnknownWorkloadError,
    WorkloadIdentitySource,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def _issuer(clock: ManualClock, ca_key: SigningKey, jwt_key: SigningKey) -> IdentityIssuer:
    issuer = IdentityIssuer.bootstrap(
        TRUST_DOMAIN, clock=clock, ca_key=ca_key, jwt_key=jwt_key, algorithm=KeyAlgorithm.EC_P256
    )
    issuer.registry.register(
        f"spiffe://{TRUST_DOMAIN}/render-worker", ["k8s:ns:render"], svid_ttl=timedelta(hours=1)
    )
    return issuer


def test_attest_and_issue(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    attestor = StaticAttestor.of("k8s:ns:render", "unix:uid:1000")
    ident = issuer.attest_and_issue(attestor)
    assert ident.spiffe_id.path == "/render-worker"
    # issued cert verifies against the issuer's own bundle
    SvidVerifier(issuer.trust_bundle(), clock=clock).verify_svid(ident.x509_svid)


def test_issue_jwt_for_id(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    from app.zerotrust.identity import JwtSvidVerifier

    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    svid = issuer.issue_jwt_for_id(f"spiffe://{TRUST_DOMAIN}/render-worker", "aud")
    decoded = JwtSvidVerifier(issuer.jwt_registry(), clock=clock).verify(
        svid.token, audience="aud"
    )
    assert decoded.spiffe_id.path == "/render-worker"


def test_issue_unknown_id_raises(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    with pytest.raises(UnknownWorkloadError):
        issuer.issue_for_id(f"spiffe://{TRUST_DOMAIN}/nope")


# --------------------------------------------------------------------------- #
# Rotation correctness
# --------------------------------------------------------------------------- #


def test_rotation_policy_threshold(clock: ManualClock) -> None:
    """renew_after = min(notBefore + fraction*lifetime, notAfter - min_remaining)."""

    root = CertificateAuthority.new_root(TRUST_DOMAIN, clock=clock)
    svid = root.issue_svid(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), ttl=timedelta(hours=1))
    pol = RotationPolicy(renew_at_fraction=0.5, min_remaining=timedelta(seconds=30))
    # not due immediately
    assert not pol.is_due(svid, clock.now())
    # due once half the lifetime has elapsed
    clock.advance(minutes=31)
    assert pol.is_due(svid, clock.now())


def test_identity_source_rotates_exactly_once_per_window(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    att = StaticAttestor.of("k8s:ns:render").attest({})
    events: list[RotationEvent] = []
    src = WorkloadIdentitySource.for_attestation(
        issuer, att, policy=RotationPolicy(renew_at_fraction=0.5), on_rotate=events.append
    )
    first = src.current()  # issues
    assert len(events) == 1
    # not yet due → same serial, no new event
    clock.advance(minutes=10)
    assert src.current().serial_number == first.serial_number
    assert len(events) == 1
    # past 50% of a 1h cert → rotates once
    clock.advance(minutes=25)
    second = src.current()
    assert second.serial_number != first.serial_number
    assert len(events) == 2
    assert events[-1].old_serial == first.serial_number
    assert events[-1].new_serial == second.serial_number


def test_identity_source_force_refresh(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    att = StaticAttestor.of("k8s:ns:render").attest({})
    src = WorkloadIdentitySource.for_attestation(issuer, att)
    a = src.current()
    b = src.refresh()
    assert a.serial_number != b.serial_number
    assert len(src.history()) == 2


def test_identity_source_needs_rotation_before_first_issue(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    att = StaticAttestor.of("k8s:ns:render").attest({})
    src = WorkloadIdentitySource.for_attestation(issuer, att)
    assert src.peek() is None
    assert src.needs_rotation()
    src.current()
    assert not src.needs_rotation()


def test_rotated_cert_is_independently_valid(
    clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey
) -> None:
    issuer = _issuer(clock, ec_ca_key, ec_jwt_key)
    att = StaticAttestor.of("k8s:ns:render").attest({})
    src = WorkloadIdentitySource.for_attestation(issuer, att)
    src.current()
    clock.advance(minutes=40)
    rotated = src.current()
    SvidVerifier(issuer.trust_bundle(), clock=clock).verify_svid(rotated)


def test_rotation_policy_validates_fraction() -> None:
    with pytest.raises(ValueError):
        RotationPolicy(renew_at_fraction=0.0)
    with pytest.raises(ValueError):
        RotationPolicy(renew_at_fraction=1.0)
