"""Attestation selector matching + workload-registry resolution tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    AttestationResult,
    IdentityError,
    Selector,
    StaticAttestor,
    TrustDomainMismatchError,
    UnknownWorkloadError,
    WorkloadRegistry,
    parse_selectors,
    selectors_satisfy,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def test_selector_parse_and_str() -> None:
    s = Selector.parse("k8s:ns:render")
    assert s.type == "k8s"
    assert s.value == "ns:render"
    assert str(s) == "k8s:ns:render"


def test_selector_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        Selector.parse("no-colon")
    with pytest.raises(ValueError):
        Selector("", "x")


def test_selectors_satisfy_subset_rule() -> None:
    required = parse_selectors(["k8s:ns:render", "k8s:sa:worker"])
    attested = parse_selectors(["k8s:ns:render", "k8s:sa:worker", "unix:uid:1000"])
    assert selectors_satisfy(required, attested)
    # missing one required selector → no
    assert not selectors_satisfy(required, parse_selectors(["k8s:ns:render"]))
    # empty required = catch-all
    assert selectors_satisfy(frozenset(), attested)


def test_static_attestor_returns_fixed_set() -> None:
    att = StaticAttestor.of("k8s:ns:render").attest({})
    assert att.has("k8s:ns:render")
    assert not att.has("k8s:ns:api")


def test_registry_most_specific_entry_wins() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    reg.register(f"spiffe://{TRUST_DOMAIN}/broad", ["k8s:ns:render"])
    reg.register(
        f"spiffe://{TRUST_DOMAIN}/specific",
        ["k8s:ns:render", "k8s:sa:worker"],
    )
    att = AttestationResult(parse_selectors(["k8s:ns:render", "k8s:sa:worker"]))
    entry = reg.require_match(att)
    assert entry.spiffe_id.path == "/specific"


def test_registry_falls_back_to_broad_entry() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    reg.register(f"spiffe://{TRUST_DOMAIN}/broad", ["k8s:ns:render"])
    reg.register(f"spiffe://{TRUST_DOMAIN}/specific", ["k8s:ns:render", "k8s:sa:worker"])
    att = AttestationResult(parse_selectors(["k8s:ns:render"]))
    assert reg.require_match(att).spiffe_id.path == "/broad"


def test_registry_no_match_raises() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    reg.register(f"spiffe://{TRUST_DOMAIN}/x", ["k8s:ns:render"])
    with pytest.raises(UnknownWorkloadError):
        reg.require_match(AttestationResult(parse_selectors(["k8s:ns:other"])))


def test_registry_rejects_foreign_domain() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    with pytest.raises(TrustDomainMismatchError):
        reg.register("spiffe://other.internal/x", [])


def test_registry_rejects_bare_domain() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    with pytest.raises(IdentityError):
        reg.register(f"spiffe://{TRUST_DOMAIN}", [])


def test_registry_replace_same_key() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    reg.register(f"spiffe://{TRUST_DOMAIN}/x", ["k8s:ns:render"], svid_ttl=timedelta(hours=1))
    reg.register(f"spiffe://{TRUST_DOMAIN}/x", ["k8s:ns:render"], svid_ttl=timedelta(hours=2))
    entries = reg.by_id(f"spiffe://{TRUST_DOMAIN}/x")
    assert len(entries) == 1
    assert entries[0].svid_ttl == timedelta(hours=2)


def test_registry_require_id_unknown() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    with pytest.raises(UnknownWorkloadError):
        reg.require_id(f"spiffe://{TRUST_DOMAIN}/missing")


def test_downstream_recorded() -> None:
    reg = WorkloadRegistry(TRUST_DOMAIN)
    entry = reg.register(
        f"spiffe://{TRUST_DOMAIN}/a",
        ["k8s:ns:a"],
        downstream=[f"spiffe://{TRUST_DOMAIN}/b"],
    )
    assert any(d.path == "/b" for d in entry.downstream)
