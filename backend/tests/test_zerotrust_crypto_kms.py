"""KMS + envelope key hierarchy correctness (fixed root key, no infra).

Asserts the envelope-encryption invariants: a fixed root makes the whole
hierarchy deterministic; DEKs are random but wrappable/unwrappable; the KEK
key-state machine gates encrypt/decrypt; rewrap rolls a DEK to the active version
without changing the underlying key; and AAD on the wrap prevents cross-KEK
replay.
"""

from __future__ import annotations

import pytest

from app.zerotrust.crypto.aead import Algorithm
from app.zerotrust.crypto.errors import (
    DecryptionError,
    KeyNotFoundError,
    KeyStateError,
)
from app.zerotrust.crypto.keys import KeyManagementService, KeyState, WrappedDek
from app.zerotrust.crypto.kms import SoftwareKMS, from_env_root_key

ROOT = bytes([0xAB]) * 32


def _kms() -> SoftwareKMS:
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    return kms


def test_software_kms_satisfies_contract() -> None:
    assert isinstance(_kms(), KeyManagementService)  # runtime Protocol check


def test_root_key_length_enforced() -> None:
    with pytest.raises(KeyStateError):
        SoftwareKMS(b"short root")


def test_generate_and_unwrap_round_trip() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")
    assert len(dk.plaintext) == 32
    assert kms.unwrap_data_key(dk.wrapped) == dk.plaintext


def test_dek_is_random_each_call() -> None:
    kms = _kms()
    a = kms.generate_data_key("pii")
    b = kms.generate_data_key("pii")
    assert a.plaintext != b.plaintext


def test_kek_derivation_is_deterministic_across_instances() -> None:
    # A fixed root reproduces the exact same KEK (so a wrapped DEK from one
    # process is unwrappable in another) — the property the tests rely on.
    kms1 = _kms()
    kms2 = _kms()
    dk = kms1.generate_data_key("pii")
    # kms2 has the same root + same kek registry, so it unwraps kms1's DEK.
    assert kms2.unwrap_data_key(dk.wrapped) == dk.plaintext


def test_unknown_kek_raises() -> None:
    kms = _kms()
    with pytest.raises(KeyNotFoundError):
        kms.generate_data_key("does-not-exist")


def test_rotate_kek_bumps_active_and_drains_old() -> None:
    kms = _kms()
    assert kms.active_version("pii") == 1
    new = kms.rotate_kek("pii")
    assert new == 2
    assert kms.active_version("pii") == 2


def test_rewrap_rolls_to_active_version_preserving_dek() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")
    assert dk.wrapped.kek_version == 1
    kms.rotate_kek("pii")
    rewrapped = kms.rewrap_data_key(dk.wrapped)
    assert rewrapped.kek_version == 2
    # The DEK bytes are identical — only the wrapping key changed.
    assert kms.unwrap_data_key(rewrapped) == dk.plaintext


def test_rewrap_is_idempotent_when_already_current() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")
    same = kms.rewrap_data_key(dk.wrapped)
    assert same == dk.wrapped  # no-op when already on the active version


def test_old_version_still_decrypts_during_drain() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")  # wrapped under v1
    kms.rotate_kek("pii")  # v1 -> PENDING_DELETION (decrypt-only)
    # Old wrapped DEK is still unwrappable while draining.
    assert kms.unwrap_data_key(dk.wrapped) == dk.plaintext


def test_destroyed_version_cannot_decrypt() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")
    kms.set_state("pii", 1, KeyState.DESTROYED)
    with pytest.raises(KeyStateError):
        kms.unwrap_data_key(dk.wrapped)


def test_disabled_kek_cannot_encrypt_but_can_decrypt() -> None:
    kms = _kms()
    dk = kms.generate_data_key("pii")
    kms.set_state("pii", 1, KeyState.DISABLED)
    with pytest.raises(KeyStateError):
        kms.generate_data_key("pii")  # encrypt path blocked
    assert kms.unwrap_data_key(dk.wrapped) == dk.plaintext  # decrypt still ok


def test_cross_kek_replay_rejected() -> None:
    kms = _kms()
    kms.register_kek("books")
    dk = kms.generate_data_key("pii")
    # Forge a wrapped DEK claiming to belong to "books" but with pii's ciphertext.
    forged = WrappedDek(
        kek_id="books",
        kek_version=1,
        ciphertext=dk.wrapped.ciphertext,
        algorithm=dk.wrapped.algorithm,
    )
    with pytest.raises(DecryptionError):
        kms.unwrap_data_key(forged)


def test_derive_purpose_key_is_stable_and_version_bound() -> None:
    kms = _kms()
    a = kms.derive_purpose_key("pii", b"column-search")
    b = kms.derive_purpose_key("pii", b"column-search")
    assert a == b and len(a) == 32
    # Different purpose -> different key (domain separation).
    assert kms.derive_purpose_key("pii", b"other") != a
    # Rotating the KEK rolls the purpose key.
    kms.rotate_kek("pii")
    assert kms.derive_purpose_key("pii", b"column-search") != a


def test_state_machine_can_decrypt_can_encrypt() -> None:
    assert KeyState.ENABLED.can_encrypt and KeyState.ENABLED.can_decrypt
    assert not KeyState.DISABLED.can_encrypt and KeyState.DISABLED.can_decrypt
    assert KeyState.PENDING_DELETION.can_decrypt
    assert not KeyState.PENDING_DELETION.can_encrypt
    assert not KeyState.DESTROYED.can_decrypt


def test_from_env_default_is_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINORA_KMS_ROOT_KEY", raising=False)
    kms = from_env_root_key()
    assert isinstance(kms, SoftwareKMS)


def test_from_env_hex_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINORA_KMS_ROOT_KEY", ROOT.hex())
    kms = from_env_root_key()
    kms.register_kek("pii")
    # Same root as the fixed-key KMS, so it unwraps an independently wrapped DEK.
    ref = _kms()
    dk = ref.generate_data_key("pii", algorithm=Algorithm.AES_256_GCM)
    assert kms.unwrap_data_key(dk.wrapped) == dk.plaintext
