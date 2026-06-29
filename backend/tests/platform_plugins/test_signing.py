"""Artifact digest + signature unit tests (deterministic, fail-closed)."""

from __future__ import annotations

import pytest

from app.platform.plugins.errors import SignatureError
from app.platform.plugins.signing import (
    Signature,
    Signer,
    artifact_digest,
    canonical_json,
    verify_signature,
)


def test_canonical_json_is_key_order_independent() -> None:
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_artifact_digest_is_stable() -> None:
    manifest = {"id": "com.a.p", "version": "1.0.0"}
    d1 = artifact_digest(manifest, "def run(p, host): return p")
    d2 = artifact_digest({"version": "1.0.0", "id": "com.a.p"}, "def run(p, host): return p")
    assert d1 == d2
    assert len(d1) == 64  # sha256 hex


def test_digest_changes_with_source() -> None:
    m = {"id": "com.a.p", "version": "1.0.0"}
    assert artifact_digest(m, "a") != artifact_digest(m, "b")


def test_digest_no_boundary_collision() -> None:
    # Length-prefixing prevents (manifest+source) concatenation collisions.
    d1 = artifact_digest({"x": "ab"}, "c")
    d2 = artifact_digest({"x": "a"}, "bc")
    assert d1 != d2


def test_sign_and_verify_roundtrip() -> None:
    signer = Signer({"acme": b"super-secret-key"})
    manifest = {"id": "com.a.p", "version": "1.0.0"}
    source = "def run(p, host): return p"
    digest = artifact_digest(manifest, source)
    sig = signer.sign(key_id="acme", digest=digest)
    # verify_signature recomputes the digest and checks the MAC.
    assert verify_signature(signer, sig, manifest=manifest, source=source) == digest


def test_tampered_source_fails_verification() -> None:
    signer = Signer({"acme": b"super-secret-key"})
    manifest = {"id": "com.a.p", "version": "1.0.0"}
    source = "def run(p, host): return p"
    sig = signer.sign(key_id="acme", digest=artifact_digest(manifest, source))
    with pytest.raises(SignatureError):
        verify_signature(signer, sig, manifest=manifest, source="def run(p, host): return 'evil'")


def test_wrong_key_fails_verification() -> None:
    signer = Signer({"acme": b"key-one"})
    digest = artifact_digest({"id": "x", "version": "1.0.0"}, "src")
    sig = signer.sign(key_id="acme", digest=digest)
    # A verifier with a different key for 'acme' rejects the signature.
    other = Signer({"acme": b"different-key"})
    with pytest.raises(SignatureError):
        other.verify(sig, expected_digest=digest)


def test_unknown_signing_key_raises() -> None:
    signer = Signer({})
    with pytest.raises(SignatureError):
        signer.sign(key_id="nope", digest="0" * 64)


def test_signature_serialization_roundtrip() -> None:
    sig = Signature(scheme="hmac-sha256", key_id="acme", digest="d" * 64, value="v")
    again = Signature.from_dict(sig.to_dict())
    assert again == sig


def test_signature_from_malformed_dict_raises() -> None:
    with pytest.raises(SignatureError):
        Signature.from_dict({"scheme": "x"})  # missing fields
    with pytest.raises(SignatureError):
        Signature.from_dict(None)


def test_digest_mismatch_in_signature_rejected() -> None:
    signer = Signer({"acme": b"k"})
    sig = signer.sign(key_id="acme", digest="a" * 64)
    with pytest.raises(SignatureError, match="does not match"):
        signer.verify(sig, expected_digest="b" * 64)
