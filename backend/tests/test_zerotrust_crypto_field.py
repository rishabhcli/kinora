"""Field-level-encryption framework + provider correctness (fixed keys, no infra).

Covers: per-record DEK encryption round-trips through every codec; AAD binds
record identity (cut-and-paste resistance across rows and columns); the stored
payload is self-contained and parses; searchable artefacts are column-stable so
two records with the same value match and a query probe matches stored rows; and
the DEK cache is transparent.
"""

from __future__ import annotations

import pytest

from app.zerotrust.crypto import blind_index as bidx
from app.zerotrust.crypto import codecs
from app.zerotrust.crypto.aead import Algorithm
from app.zerotrust.crypto.context import AssociatedData, CryptoProvider
from app.zerotrust.crypto.errors import DecryptionError
from app.zerotrust.crypto.field import FieldEncryptor, FieldSpec, StoredField
from app.zerotrust.crypto.kms import SoftwareKMS

ROOT = bytes([0x5A]) * 32


def _provider() -> CryptoProvider:
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    return CryptoProvider(kms, kek_id="pii")


def _fe() -> FieldEncryptor:
    return FieldEncryptor(_provider())


def test_string_round_trip() -> None:
    fe = _fe()
    spec = FieldSpec()
    aad = AssociatedData("users", "name", "u1")
    blob, _ = fe.encrypt(spec, "Grace Hopper", aad)
    assert fe.decrypt(spec, blob, aad) == "Grace Hopper"


@pytest.mark.parametrize(
    ("codec", "value"),
    [
        (codecs.STRING, "héllo wörld"),
        (codecs.INT, 1234567),
        (codecs.BYTES, b"\x00\x01\x02\xff"),
        (codecs.JSON, {"b": 2, "a": [1, 2, 3]}),
    ],
)
def test_codec_round_trips(codec: object, value: object) -> None:
    fe = _fe()
    spec = FieldSpec(codec=codec, normalizer="identity")  # type: ignore[arg-type]
    aad = AssociatedData("t", "c", "r")
    blob, _ = fe.encrypt(spec, value, aad)
    assert fe.decrypt(spec, blob, aad) == value


def test_aad_binds_record_identity() -> None:
    fe = _fe()
    spec = FieldSpec()
    blob, _ = fe.encrypt(spec, "secret", AssociatedData("users", "ssn", "u1"))
    # Wrong row -> integrity failure.
    with pytest.raises(DecryptionError):
        fe.decrypt(spec, blob, AssociatedData("users", "ssn", "u2"))
    # Wrong column -> integrity failure.
    with pytest.raises(DecryptionError):
        fe.decrypt(spec, blob, AssociatedData("users", "email", "u1"))
    # Wrong table -> integrity failure.
    with pytest.raises(DecryptionError):
        fe.decrypt(spec, blob, AssociatedData("orders", "ssn", "u1"))


def test_each_record_uses_a_distinct_dek() -> None:
    fe = _fe()
    spec = FieldSpec()
    b1, _ = fe.encrypt(spec, "x", AssociatedData("t", "c", "r1"))
    b2, _ = fe.encrypt(spec, "x", AssociatedData("t", "c", "r2"))
    w1 = StoredField.from_bytes(b1).ciphertext.wrapped_dek
    w2 = StoredField.from_bytes(b2).ciphertext.wrapped_dek
    assert w1.ciphertext != w2.ciphertext  # different wrapped DEKs


def test_stored_payload_is_self_contained() -> None:
    fe = _fe()
    spec = FieldSpec()
    blob, _ = fe.encrypt(spec, "value", AssociatedData("t", "c", "r"))
    parsed = StoredField.from_bytes(blob)
    assert parsed.to_bytes() == blob
    assert parsed.ciphertext.wrapped_dek.kek_id == "pii"


def test_corrupt_payload_raises() -> None:
    fe = _fe()
    with pytest.raises(DecryptionError):
        fe.decrypt(FieldSpec(), b"\x05not-json-meta", AssociatedData("t", "c", "r"))


def test_searchable_equality_is_column_stable() -> None:
    fe = _fe()
    spec = FieldSpec(searchable_equality=True, normalizer="casefold")
    _, a1 = fe.encrypt(spec, "Alice", AssociatedData("u", "name", "r1"))
    _, a2 = fe.encrypt(spec, "alice", AssociatedData("u", "name", "r2"))
    assert a1.deterministic == a2.deterministic  # equal value -> equal det ct
    probe = fe.search_tokens(spec, "ALICE")
    assert probe.deterministic == a1.deterministic  # query probe matches


def test_blind_equality_token_matches_probe() -> None:
    fe = _fe()
    spec = FieldSpec(blind_equality=True, normalizer="casefold")
    _, art = fe.encrypt(spec, "Bob", AssociatedData("u", "name", "r1"))
    probe = fe.search_tokens(spec, "bob")
    assert probe.equality_index == art.equality_index


def test_blind_prefix_tokens_support_like() -> None:
    fe = _fe()
    provider = _provider()
    fe = FieldEncryptor(provider)
    spec = FieldSpec(blind_prefix=True, normalizer="casefold", prefix_max_len=8)
    _, art = fe.encrypt(spec, "Cassidy", AssociatedData("u", "name", "r1"))
    # Reconstruct the key the same way the encryptor does, to build the probe.
    det_key, bidx_key, _ = fe._search_keys(spec)
    assert bidx.prefix_query_token(bidx_key, b"cas") in art.prefix_indexes
    assert bidx.prefix_query_token(bidx_key, b"zzz") not in art.prefix_indexes


def test_blind_range_bucket() -> None:
    fe = _fe()
    spec = FieldSpec(
        codec=codecs.INT, blind_range=True, range_bucket_size=10, normalizer="identity"
    )
    _, art = fe.encrypt(spec, 47, AssociatedData("u", "age", "r1"))
    _, bidx_key, _ = fe._search_keys(spec)
    assert art.range_bucket in bidx.buckets_for_range(bidx_key, 40, 49, bucket_size=10)
    assert art.range_bucket not in bidx.buckets_for_range(bidx_key, 50, 59, bucket_size=10)


def test_email_normalizer_domain_case_insensitive() -> None:
    fe = _fe()
    spec = FieldSpec(searchable_equality=True, normalizer="email")
    _, a = fe.encrypt(spec, "Bob@Example.COM", AssociatedData("u", "e", "r1"))
    _, b = fe.encrypt(spec, "Bob@example.com", AssociatedData("u", "e", "r2"))
    assert a.deterministic == b.deterministic  # domain folded
    _, c = fe.encrypt(spec, "bob@example.com", AssociatedData("u", "e", "r3"))
    assert c.deterministic != a.deterministic  # local part case-sensitive


def test_unknown_normalizer_rejected() -> None:
    with pytest.raises(KeyError):
        FieldSpec(normalizer="not-a-real-normalizer")


def test_dek_cache_round_trips_and_clears() -> None:
    provider = _provider()
    fe = FieldEncryptor(provider)
    spec = FieldSpec()
    aad = AssociatedData("t", "c", "r")
    blob, _ = fe.encrypt(spec, "cached", aad)
    assert fe.decrypt(spec, blob, aad) == "cached"  # served from cache
    provider.clear_cache()
    assert fe.decrypt(spec, blob, aad) == "cached"  # re-unwrapped from KMS


def test_algorithm_choice_is_honoured() -> None:
    fe = _fe()
    spec = FieldSpec(algorithm=Algorithm.CHACHA20_POLY1305)
    aad = AssociatedData("t", "c", "r")
    blob, _ = fe.encrypt(spec, "chacha", aad)
    parsed = StoredField.from_bytes(blob)
    assert parsed.ciphertext.wrapped_dek.algorithm == Algorithm.CHACHA20_POLY1305
    assert fe.decrypt(spec, blob, aad) == "chacha"
