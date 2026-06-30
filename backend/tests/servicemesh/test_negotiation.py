"""Capability/version negotiation between roles."""

from __future__ import annotations

import pytest

from app.servicemesh.errors import NegotiationError
from app.servicemesh.negotiation import (
    Capability,
    CapabilityRegistry,
    RoleManifest,
    negotiate,
)
from app.servicemesh.roles import ProducerRole
from app.servicemesh.versioning import SemVer, VersionRange

SCHEMA = "shot.render.job"


def _producer(produces: str, preferred: str | None = None) -> RoleManifest:
    return RoleManifest(
        role=ProducerRole.API,
        capabilities=(
            Capability(
                SCHEMA,
                produces=VersionRange.parse(produces),
                preferred=SemVer.parse(preferred) if preferred else None,
            ),
        ),
    )


def _consumer(consumes: str) -> RoleManifest:
    return RoleManifest(
        role=ProducerRole.RENDER_WORKER,
        capabilities=(Capability(SCHEMA, consumes=VersionRange.parse(consumes)),),
    )


def test_negotiates_highest_known_in_overlap() -> None:
    prod = _producer(">=1.0.0,<3.0.0")
    cons = _consumer(">=2.0.0,<4.0.0")
    known = [SemVer.parse(v) for v in ("1.0.0", "2.0.0", "2.5.0", "3.0.0")]
    result = negotiate(prod, cons, SCHEMA, known_versions=known)
    # overlap = [2.0.0, 3.0.0) -> highest known inside is 2.5.0.
    assert result.agreed_version == SemVer.parse("2.5.0")
    assert result.producer is ProducerRole.API
    assert result.consumer is ProducerRole.RENDER_WORKER


def test_prefers_producer_preference_in_overlap() -> None:
    prod = _producer(">=1.0.0,<3.0.0", preferred="2.0.0")
    cons = _consumer(">=2.0.0,<4.0.0")
    known = [SemVer.parse(v) for v in ("2.0.0", "2.5.0")]
    result = negotiate(prod, cons, SCHEMA, known_versions=known)
    assert result.agreed_version == SemVer.parse("2.0.0")


def test_preference_outside_overlap_is_ignored() -> None:
    prod = _producer(">=1.0.0,<3.0.0", preferred="1.0.0")
    cons = _consumer(">=2.0.0,<4.0.0")
    known = [SemVer.parse(v) for v in ("2.0.0", "2.5.0")]
    result = negotiate(prod, cons, SCHEMA, known_versions=known)
    # 1.0.0 is outside [2.0.0,3.0.0) -> falls back to highest known = 2.5.0.
    assert result.agreed_version == SemVer.parse("2.5.0")


def test_falls_back_to_overlap_lower_bound_without_known() -> None:
    prod = _producer(">=1.0.0,<3.0.0")
    cons = _consumer(">=2.0.0,<4.0.0")
    result = negotiate(prod, cons, SCHEMA)
    assert result.agreed_version == SemVer.parse("2.0.0")  # overlap lower bound


def test_disjoint_ranges_raise() -> None:
    prod = _producer(">=1.0.0,<2.0.0")
    cons = _consumer(">=3.0.0,<4.0.0")
    with pytest.raises(NegotiationError):
        negotiate(prod, cons, SCHEMA)


def test_producer_missing_schema_raises() -> None:
    prod = RoleManifest(role=ProducerRole.API, capabilities=())
    cons = _consumer(">=1.0.0")
    with pytest.raises(NegotiationError):
        negotiate(prod, cons, SCHEMA)


def test_consumer_missing_schema_raises() -> None:
    prod = _producer(">=1.0.0")
    cons = RoleManifest(role=ProducerRole.RENDER_WORKER, capabilities=())
    with pytest.raises(NegotiationError):
        negotiate(prod, cons, SCHEMA)


def test_capability_requires_a_range() -> None:
    with pytest.raises(NegotiationError):
        Capability(SCHEMA)  # neither produces nor consumes


def test_capability_preference_must_be_in_own_range() -> None:
    with pytest.raises(NegotiationError):
        Capability(
            SCHEMA,
            produces=VersionRange.parse(">=1.0.0,<2.0.0"),
            preferred=SemVer.parse("5.0.0"),
        )


def test_capability_registry_advertise_and_negotiate() -> None:
    reg = CapabilityRegistry()
    reg.advertise(
        ProducerRole.API,
        Capability(SCHEMA, produces=VersionRange.parse(">=1.0.0,<3.0.0")),
    )
    reg.advertise(
        ProducerRole.RENDER_WORKER,
        Capability(SCHEMA, consumes=VersionRange.parse(">=2.0.0,<4.0.0")),
    )
    result = reg.negotiate(ProducerRole.API, ProducerRole.RENDER_WORKER, SCHEMA)
    assert result.agreed_version == SemVer.parse("2.0.0")
    # The manifest reflects what was advertised.
    manifest = reg.manifest(ProducerRole.API)
    assert manifest.by_schema(SCHEMA) is not None
