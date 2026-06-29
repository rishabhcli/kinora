"""KMS envelope encryption, key versioning, rotation, and re-wrap tests."""

from __future__ import annotations

import dataclasses

import pytest

from app.zerotrust.identity import (
    DecryptionError,
    EnvelopeCipher,
    KeyNotFoundError,
    KeyState,
    KmsError,
    LocalKms,
    ManualClock,
    WrappedKey,
)
from app.zerotrust.identity.errors import KeyDisabledError, KeyStateError
from tests.zerotrust.conftest import FIXED_KEK


def _kms(clock: ManualClock) -> LocalKms:
    kms = LocalKms(clock=clock)
    kms.create_key("kek", material=FIXED_KEK)
    return kms


def test_wrap_unwrap_round_trip(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"secret-bytes")
    assert kms.decrypt(wrapped) == b"secret-bytes"


def test_aad_binding(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"x", aad=b"ctx:1")
    assert kms.decrypt(wrapped, aad=b"ctx:1") == b"x"
    with pytest.raises(DecryptionError):
        kms.decrypt(wrapped, aad=b"ctx:2")


def test_tampered_ciphertext_rejected(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"x")
    bad = dataclasses.replace(
        wrapped, ciphertext=wrapped.ciphertext[:-1] + bytes([wrapped.ciphertext[-1] ^ 1])
    )
    with pytest.raises(DecryptionError):
        kms.decrypt(bad)


def test_cross_key_unwrap_fails(clock: ManualClock) -> None:
    kms = _kms(clock)
    kms.create_key("other", material=bytes(reversed(FIXED_KEK)))
    wrapped = kms.encrypt("kek", b"x")
    # forge the key id to point at 'other' — AAD binds key id so it fails
    forged = dataclasses.replace(wrapped, key_id="other")
    with pytest.raises(DecryptionError):
        kms.decrypt(forged)


def test_generate_data_key(clock: ManualClock) -> None:
    kms = _kms(clock)
    dk = kms.generate_data_key("kek", length=32)
    assert len(dk.plaintext) == 32
    # the wrapped DEK unwraps back to the same plaintext
    assert kms.decrypt(dk.wrapped) == dk.plaintext


def test_envelope_seal_open(clock: ManualClock) -> None:
    kms = _kms(clock)
    cipher = EnvelopeCipher(kms, "kek")
    sealed = cipher.seal(b"the-canon-vault", aad=b"book:42")
    assert cipher.open_(sealed) == b"the-canon-vault"
    # tamper rejection on the payload
    bad = dataclasses.replace(
        sealed, ciphertext=sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1])
    )
    with pytest.raises(DecryptionError):
        cipher.open_(bad)


def test_envelope_aad_mismatch(clock: ManualClock) -> None:
    kms = _kms(clock)
    cipher = EnvelopeCipher(kms, "kek")
    sealed = cipher.seal(b"x", aad=b"book:1")
    bad = dataclasses.replace(sealed, aad=b"book:2")
    with pytest.raises(DecryptionError):
        cipher.open_(bad)


# --------------------------------------------------------------------------- #
# Versioning + rotation + re-wrap
# --------------------------------------------------------------------------- #


def test_rotation_keeps_old_versions_usable(clock: ManualClock) -> None:
    kms = _kms(clock)
    v1_wrapped = kms.encrypt("kek", b"old-secret")
    assert kms.current_version("kek") == 1
    v2 = kms.rotate_key("kek")
    assert v2 == 2
    assert kms.current_version("kek") == 2
    # ciphertext minted under v1 STILL decrypts after rotation
    assert kms.decrypt(v1_wrapped) == b"old-secret"
    # new ciphertext goes under v2
    v2_wrapped = kms.encrypt("kek", b"new-secret")
    assert v2_wrapped.version == 2


def test_needs_rewrap_and_rewrap(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"secret")
    assert not kms.needs_rewrap(wrapped)
    kms.rotate_key("kek")
    assert kms.needs_rewrap(wrapped)
    rewrapped = kms.rewrap(wrapped)
    assert rewrapped.version == 2
    assert not kms.needs_rewrap(rewrapped)
    # the re-wrapped blob still decrypts to the SAME plaintext
    assert kms.decrypt(rewrapped) == b"secret"


def test_rewrap_with_aad(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"s", aad=b"path/x")
    kms.rotate_key("kek")
    rewrapped = kms.rewrap(wrapped, aad=b"path/x")
    assert kms.decrypt(rewrapped, aad=b"path/x") == b"s"


def test_disabled_version_cannot_unwrap(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"x")
    kms.rotate_key("kek")  # so v1 isn't the only usable version
    kms.disable_version("kek", 1)
    with pytest.raises(KeyDisabledError):
        kms.decrypt(wrapped)
    kms.enable_version("kek", 1)
    assert kms.decrypt(wrapped) == b"x"


def test_destroy_version_zeroises(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"x")
    kms.rotate_key("kek")
    kms.destroy_version("kek", 1)
    with pytest.raises(KeyDisabledError):
        kms.decrypt(wrapped)


def test_cannot_destroy_only_usable_version(clock: ManualClock) -> None:
    kms = _kms(clock)
    with pytest.raises(KeyStateError):
        kms.destroy_version("kek", 1)


def test_missing_key_and_version(clock: ManualClock) -> None:
    kms = _kms(clock)
    with pytest.raises(KeyNotFoundError):
        kms.encrypt("nope", b"x")
    wrapped = kms.encrypt("kek", b"x")
    bad = dataclasses.replace(wrapped, version=99)
    with pytest.raises(KeyNotFoundError):
        kms.decrypt(bad)


def test_duplicate_create_rejected(clock: ManualClock) -> None:
    kms = _kms(clock)
    with pytest.raises(KmsError):
        kms.create_key("kek")


def test_bad_kek_material_length(clock: ManualClock) -> None:
    kms = LocalKms(clock=clock)
    with pytest.raises(KmsError):
        kms.create_key("k", material=b"too-short")


def test_wrapped_key_token_round_trip(clock: ManualClock) -> None:
    kms = _kms(clock)
    wrapped = kms.encrypt("kek", b"x")
    token = wrapped.to_token()
    restored = WrappedKey.from_token(token)
    assert restored == wrapped
    assert kms.decrypt(restored) == b"x"


def test_key_state_enum() -> None:
    assert KeyState.ENABLED == "enabled"
    assert KeyState("disabled") is KeyState.DISABLED


def test_with_key_helper(clock: ManualClock) -> None:
    kms = LocalKms.with_key("seeded", FIXED_KEK, clock=clock)
    assert kms.decrypt(kms.encrypt("seeded", b"x")) == b"x"
