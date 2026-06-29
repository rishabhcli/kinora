"""End-to-end IdentityFabric + contract-conformance tests.

These exercise the whole zero-trust loop a server would run: attest a workload,
issue its SVID, present it, verify the chain, and policy-check the call — plus
the secret/KMS conveniences the fabric bundles.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    AuthorizationError,
    AuthorizationGate,
    IdentityFabric,
    IdentityProvider,
    KeyManagementService,
    PeerVerificationError,
    PeerVerifier,
    SecretProvider,
    StaticAttestor,
    TokenVerifier,
)


def test_authorized_handshake_happy_path(fabric: IdentityFabric) -> None:
    fabric.register(
        "spiffe://acme.kinora.internal/render-worker",
        ["k8s:ns:render"],
        downstream=["spiffe://acme.kinora.internal/mcp"],
    )
    fabric.register("spiffe://acme.kinora.internal/mcp", ["k8s:ns:mcp"])
    ident = fabric.issue(StaticAttestor.of("k8s:ns:render").attest({}))
    peer = fabric.authorized_handshake(
        ident.x509_svid, target="spiffe://acme.kinora.internal/mcp"
    )
    assert peer.spiffe_id.path == "/render-worker"


def test_authorized_handshake_denies_unregistered_target(fabric: IdentityFabric) -> None:
    fabric.register(
        "spiffe://acme.kinora.internal/render-worker",
        ["k8s:ns:render"],
        downstream=["spiffe://acme.kinora.internal/mcp"],
    )
    ident = fabric.issue(StaticAttestor.of("k8s:ns:render").attest({}))
    with pytest.raises(AuthorizationError):
        fabric.authorized_handshake(
            ident.x509_svid, target="spiffe://acme.kinora.internal/secret-vault"
        )


def test_authorized_handshake_rejects_expired_cert(fabric: IdentityFabric) -> None:
    fabric.register(
        "spiffe://acme.kinora.internal/a",
        ["k8s:ns:a"],
        svid_ttl=timedelta(hours=1),
        downstream=["spiffe://acme.kinora.internal/b"],
    )
    fabric.register("spiffe://acme.kinora.internal/b", ["k8s:ns:b"])
    ident = fabric.issue(StaticAttestor.of("k8s:ns:a").attest({}))
    fabric.clock.advance(hours=2)  # type: ignore[attr-defined]
    with pytest.raises(PeerVerificationError):
        fabric.authorized_handshake(
            ident.x509_svid, target="spiffe://acme.kinora.internal/b"
        )


def test_authorize_token_path(fabric: IdentityFabric) -> None:
    fabric.register(
        "spiffe://acme.kinora.internal/a",
        ["k8s:ns:a"],
        downstream=["spiffe://acme.kinora.internal/b"],
    )
    fabric.register("spiffe://acme.kinora.internal/b", ["k8s:ns:b"])
    jwt = fabric.issuer.issue_jwt_for_id(
        "spiffe://acme.kinora.internal/a", "spiffe://acme.kinora.internal/b"
    )
    decoded = fabric.authorize_token(
        jwt.token,
        target="spiffe://acme.kinora.internal/b",
        audience="spiffe://acme.kinora.internal/b",
    )
    assert decoded.spiffe_id.path == "/a"


def test_decide_is_policy_only(fabric: IdentityFabric) -> None:
    fabric.register(
        "spiffe://acme.kinora.internal/a",
        ["k8s:ns:a"],
        downstream=["spiffe://acme.kinora.internal/b"],
    )
    assert fabric.decide(
        "spiffe://acme.kinora.internal/a", "spiffe://acme.kinora.internal/b"
    ).allowed
    assert not fabric.decide(
        "spiffe://acme.kinora.internal/x", "spiffe://acme.kinora.internal/b"
    ).allowed


def test_fabric_secret_store_sealed(fabric: IdentityFabric) -> None:
    fabric.secrets.put("providers/dashscope", {"api_key": "sk-xyz"})
    assert fabric.secrets.get_map("providers/dashscope")["api_key"] == "sk-xyz"


def test_fabric_kek_rotation_then_rewrap(fabric: IdentityFabric) -> None:
    from app.zerotrust.identity import DEFAULT_KEK_ID

    fabric.secrets.put("p", {"v": "1"})
    fabric.kms.rotate_key(DEFAULT_KEK_ID)
    assert fabric.secrets.rewrap_all() == 1
    assert fabric.secrets.get_map("p")["v"] == "1"


def test_contracts_are_satisfied_structurally(fabric: IdentityFabric) -> None:
    """The concrete classes satisfy the Protocols sibling facets depend on."""

    assert isinstance(fabric.issuer, IdentityProvider)
    assert isinstance(fabric.verifier(), PeerVerifier)
    assert isinstance(fabric.token_verifier(), TokenVerifier)
    assert isinstance(fabric.secrets, SecretProvider)
    assert isinstance(fabric.policy, AuthorizationGate)
    assert isinstance(fabric.kms, KeyManagementService)
