"""AEAD primitive + envelope wire-format correctness (fixed keys, no infra).

Crypto-correctness invariants asserted here:

* round-trip for every algorithm;
* AES-GCM-SIV is deterministic under a fixed nonce (the property the searchable
  layer depends on) while AES-GCM / ChaCha20 are randomised;
* AAD is authenticated — any mismatch fails the tag;
* the envelope is self-describing and parses strictly (tamper/truncation caught);
* nonce footguns are rejected (supplied nonce only for misuse-resistant algos).
"""

from __future__ import annotations

import pytest

from app.zerotrust.crypto import aead
from app.zerotrust.crypto.aead import Algorithm, Envelope
from app.zerotrust.crypto.errors import CryptoConfigError, DecryptionError

# A fixed 32-byte key so failures are reproducible.
KEY = bytes(range(32))
ALL_ALGOS = [Algorithm.AES_256_GCM, Algorithm.CHACHA20_POLY1305, Algorithm.AES_256_GCM_SIV]


@pytest.mark.parametrize("algorithm", ALL_ALGOS)
def test_round_trip(algorithm: Algorithm) -> None:
    blob = aead.seal(KEY, b"the quick brown fox", aad=b"ctx", algorithm=algorithm)
    assert aead.open_(KEY, blob, aad=b"ctx") == b"the quick brown fox"


@pytest.mark.parametrize("algorithm", ALL_ALGOS)
def test_empty_plaintext_round_trips(algorithm: Algorithm) -> None:
    blob = aead.seal(KEY, b"", aad=b"", algorithm=algorithm)
    assert aead.open_(KEY, blob, aad=b"") == b""


def test_gcm_is_randomised() -> None:
    a = aead.seal(KEY, b"same", algorithm=Algorithm.AES_256_GCM)
    b = aead.seal(KEY, b"same", algorithm=Algorithm.AES_256_GCM)
    assert a != b  # random nonce per call


def test_gcm_siv_deterministic_under_fixed_nonce() -> None:
    nonce = bytes(12)
    a = aead.seal(KEY, b"same", algorithm=Algorithm.AES_256_GCM_SIV, nonce=nonce)
    b = aead.seal(KEY, b"same", algorithm=Algorithm.AES_256_GCM_SIV, nonce=nonce)
    assert a == b  # the property the deterministic-encryption layer relies on


@pytest.mark.parametrize("algorithm", ALL_ALGOS)
def test_aad_mismatch_fails(algorithm: Algorithm) -> None:
    blob = aead.seal(KEY, b"secret", aad=b"correct", algorithm=algorithm)
    with pytest.raises(DecryptionError):
        aead.open_(KEY, blob, aad=b"wrong")


@pytest.mark.parametrize("algorithm", ALL_ALGOS)
def test_wrong_key_fails(algorithm: Algorithm) -> None:
    blob = aead.seal(KEY, b"secret", algorithm=algorithm)
    with pytest.raises(DecryptionError):
        aead.open_(bytes(32), blob)


@pytest.mark.parametrize("algorithm", ALL_ALGOS)
def test_ciphertext_tamper_fails(algorithm: Algorithm) -> None:
    blob = bytearray(aead.seal(KEY, b"secret", algorithm=algorithm))
    blob[-1] ^= 0x01  # flip a tag bit
    with pytest.raises(DecryptionError):
        aead.open_(KEY, bytes(blob))


def test_envelope_is_self_describing() -> None:
    blob = aead.seal(KEY, b"x", algorithm=Algorithm.CHACHA20_POLY1305)
    env = Envelope.from_bytes(blob)
    assert env.algorithm == Algorithm.CHACHA20_POLY1305
    assert env.version == aead.ENVELOPE_VERSION
    assert len(env.nonce) == aead.NONCE_LEN
    assert env.to_bytes() == blob  # round-trips byte-for-byte


def test_envelope_rejects_alien_bytes() -> None:
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(b"not an envelope at all, just junk bytes here!!")


def test_envelope_rejects_bad_magic() -> None:
    blob = bytearray(aead.seal(KEY, b"x"))
    blob[0] = ord("X")  # corrupt the magic
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(bytes(blob))


def test_envelope_rejects_unknown_version() -> None:
    blob = bytearray(aead.seal(KEY, b"x"))
    blob[2] = 99  # version byte
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(bytes(blob))


def test_envelope_rejects_unknown_algorithm() -> None:
    blob = bytearray(aead.seal(KEY, b"x"))
    blob[3] = 200  # algorithm byte
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(bytes(blob))


def test_envelope_rejects_truncation() -> None:
    blob = aead.seal(KEY, b"x")
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(blob[:6])  # cut mid-nonce


def test_bad_key_length_rejected() -> None:
    with pytest.raises(CryptoConfigError):
        aead.seal(b"too short", b"x")


def test_supplied_nonce_rejected_for_randomised_algorithm() -> None:
    # A caller-supplied nonce under plain GCM is a footgun — must be refused.
    with pytest.raises(CryptoConfigError):
        aead.seal(KEY, b"x", algorithm=Algorithm.AES_256_GCM, nonce=bytes(12))


def test_supplied_nonce_wrong_length_rejected() -> None:
    with pytest.raises(CryptoConfigError):
        aead.seal(KEY, b"x", algorithm=Algorithm.AES_256_GCM_SIV, nonce=b"short")


def test_generate_key_length() -> None:
    assert len(aead.generate_key()) == aead.KEY_LEN
    assert aead.generate_key() != aead.generate_key()
