"""SPIFFE ID parsing/validation + trust-domain relationship tests."""

from __future__ import annotations

import pytest

from app.zerotrust.identity import (
    InvalidSpiffeIdError,
    SpiffeId,
    TrustDomain,
    TrustDomainMismatchError,
)


def test_parse_round_trips_canonical_uri() -> None:
    sid = SpiffeId.parse("spiffe://acme.internal/agents/critic")
    assert sid.trust_domain == "acme.internal"
    assert sid.path == "/agents/critic"
    assert sid.uri == "spiffe://acme.internal/agents/critic"
    assert str(sid) == sid.uri
    assert sid.segments == ("agents", "critic")
    assert not sid.is_trust_domain


def test_parse_bare_trust_domain() -> None:
    sid = SpiffeId.parse("spiffe://acme.internal")
    assert sid.is_trust_domain
    assert sid.path == ""
    assert sid.segments == ()


def test_scheme_is_case_insensitive_but_normalised() -> None:
    sid = SpiffeId.parse("SPIFFE://Acme.Internal/Worker")
    assert sid.trust_domain == "acme.internal"
    assert sid.uri == "spiffe://acme.internal/Worker"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "http://acme.internal/x",
        "spiffe:/acme.internal/x",
        "spiffe://acme.internal:8443/x",  # no port
        "spiffe://user@acme.internal/x",  # no userinfo
        "spiffe://acme.internal/a//b",  # empty segment
        "spiffe://acme.internal/a/../b",  # dot segment
        "spiffe://acme.internal/a/",  # trailing slash
        "spiffe://acme.internal/a b",  # bad char
        "spiffe://-bad-.internal/x",  # bad label
    ],
)
def test_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidSpiffeIdError):
        SpiffeId.parse(bad)


def test_try_parse_returns_none_for_bad() -> None:
    assert SpiffeId.try_parse("not-a-spiffe-id") is None
    assert SpiffeId.try_parse("spiffe://acme.internal/x") is not None


def test_too_long_is_rejected() -> None:
    long_path = "/" + "/".join("seg" for _ in range(700))
    with pytest.raises(InvalidSpiffeIdError):
        SpiffeId("acme.internal", long_path)


def test_is_under_uses_segment_boundaries_not_string_prefix() -> None:
    prefix = SpiffeId("acme.internal", "/agents")
    assert SpiffeId("acme.internal", "/agents/critic").is_under(prefix)
    assert SpiffeId("acme.internal", "/agents").is_under(prefix)
    # the classic prefix-confusion case must NOT match
    assert not SpiffeId("acme.internal", "/agents-evil").is_under(prefix)
    # different domain never matches
    assert not SpiffeId("other.internal", "/agents/critic").is_under(prefix)


def test_member_of_and_require_domain() -> None:
    sid = SpiffeId.parse("spiffe://acme.internal/x")
    assert sid.member_of("acme.internal")
    assert sid.member_of(TrustDomain("acme.internal"))
    assert not sid.member_of("other.internal")
    sid.require_domain("acme.internal")
    with pytest.raises(TrustDomainMismatchError):
        sid.require_domain("other.internal")


def test_trust_domain_workload_factory() -> None:
    td = TrustDomain("acme.internal")
    assert td.id == "spiffe://acme.internal"
    sid = td.workload("/render-worker")
    assert sid.uri == "spiffe://acme.internal/render-worker"


def test_spiffe_id_is_hashable_and_frozen() -> None:
    import dataclasses

    a = SpiffeId("acme.internal", "/x")
    b = SpiffeId("acme.internal", "/x")
    assert a == b
    assert {a, b} == {a}
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.path = "/y"  # type: ignore[misc]
