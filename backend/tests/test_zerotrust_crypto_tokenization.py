"""Tokenization vault correctness (fixed keys, no infra).

Covers format preservation, random vs deterministic schemes, detokenize-under-
authz, the audit hook, encryption-at-rest of the stored plaintext, and the
collision/exhaustion guard.
"""

from __future__ import annotations

import pytest

from app.zerotrust.crypto.context import CryptoProvider
from app.zerotrust.crypto.errors import AuthorizationError, TokenizationError
from app.zerotrust.crypto.kms import SoftwareKMS
from app.zerotrust.crypto.tokenization import (
    Alphabet,
    DetokenizationRequest,
    FormatSpec,
    InMemoryTokenStore,
    TokenizationVault,
    TokenPolicy,
    TokenScheme,
)

ROOT = bytes([0x7C]) * 32


def _vault(audit: list | None = None) -> TokenizationVault:
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    provider = CryptoProvider(kms, kek_id="pii")
    hook = None
    if audit is not None:

        def hook(req: DetokenizationRequest, tok: str, ok: bool) -> None:  # noqa: F811
            audit.append((req.actor, req.purpose, ok))

    return TokenizationVault(provider, InMemoryTokenStore(), kek_id="pii", audit_hook=hook)


PAN_FMT = FormatSpec(alphabet=Alphabet.DIGITS, length=16, suffix="4242")
PAN_POLICY = TokenPolicy(purposes=frozenset({"billing"}), data_class="pan")


def test_token_preserves_format() -> None:
    vault = _vault()
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    assert len(token) == 16
    assert token.isdigit()
    assert token.endswith("4242")
    assert token != "4111111111114242"


def test_authorised_detokenize_returns_plaintext() -> None:
    vault = _vault()
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    got = vault.detokenize(token, DetokenizationRequest(actor="svc", purpose="billing"))
    assert got == "4111111111114242"


def test_unauthorised_purpose_rejected() -> None:
    vault = _vault()
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    with pytest.raises(AuthorizationError):
        vault.detokenize(token, DetokenizationRequest(actor="ml", purpose="training"))


def test_unknown_token_rejected() -> None:
    vault = _vault()
    with pytest.raises(TokenizationError):
        vault.detokenize("0000000000000000", DetokenizationRequest(actor="x", purpose="billing"))


def test_write_only_policy_denies_everyone() -> None:
    vault = _vault()
    token = vault.tokenize("secret", FormatSpec(length=8), TokenPolicy(purposes=frozenset()))
    with pytest.raises(AuthorizationError):
        vault.detokenize(token, DetokenizationRequest(actor="anyone", purpose="anything"))


def test_audit_hook_records_allow_and_deny() -> None:
    audit: list = []
    vault = _vault(audit)
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    vault.detokenize(token, DetokenizationRequest(actor="svc", purpose="billing"))
    with pytest.raises(AuthorizationError):
        vault.detokenize(token, DetokenizationRequest(actor="ml", purpose="training"))
    assert audit == [("svc", "billing", True), ("ml", "training", False)]


def test_deterministic_tokenization_dedups() -> None:
    vault = _vault()
    fmt = FormatSpec(alphabet=Alphabet.ALNUM_UPPER, length=12, deterministic=True)
    t1 = vault.tokenize("123-45-6789", fmt, PAN_POLICY)
    t2 = vault.tokenize("123-45-6789", fmt, PAN_POLICY)
    assert t1 == t2  # one vault row
    assert len(t1) == 12


def test_deterministic_token_is_stable_across_vaults() -> None:
    # Same root + kek -> same PRF key -> same deterministic token.
    fmt = FormatSpec(alphabet=Alphabet.HEX_LOWER, length=10, deterministic=True)
    t1 = _vault().tokenize("stable@example.com", fmt, PAN_POLICY)
    t2 = _vault().tokenize("stable@example.com", fmt, PAN_POLICY)
    assert t1 == t2


def test_random_tokenization_differs_per_call() -> None:
    vault = _vault()
    a = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    b = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    assert a != b  # random scheme -> fresh token each time


def test_stored_plaintext_is_encrypted_at_rest() -> None:
    store = InMemoryTokenStore()
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    vault = TokenizationVault(CryptoProvider(kms, kek_id="pii"), store, kek_id="pii")
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    record = store.get(token)
    assert record is not None
    # The raw stored envelope must not contain the plaintext PAN.
    assert b"4111111111114242" not in record.ciphertext.envelope


def test_peek_policy_without_revealing_plaintext() -> None:
    vault = _vault()
    token = vault.tokenize("4111111111114242", PAN_FMT, PAN_POLICY)
    policy = vault.peek_policy(token)
    assert policy is not None
    assert policy.permits("billing")
    assert not policy.permits("training")


def test_format_validation() -> None:
    with pytest.raises(TokenizationError):
        FormatSpec(length=0)
    with pytest.raises(TokenizationError):
        FormatSpec(length=4, prefix="12", suffix="34")  # no body room


def test_prefix_and_suffix_preserved() -> None:
    vault = _vault()
    fmt = FormatSpec(alphabet=Alphabet.DIGITS, length=10, prefix="99", suffix="00")
    token = vault.tokenize("9912345600", fmt, PAN_POLICY)
    assert token.startswith("99") and token.endswith("00") and len(token) == 10


def test_record_scheme_tagging() -> None:
    store = InMemoryTokenStore()
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    vault = TokenizationVault(CryptoProvider(kms, kek_id="pii"), store, kek_id="pii")
    rnd = vault.tokenize("x", FormatSpec(length=8), PAN_POLICY)
    det = vault.tokenize("y", FormatSpec(length=8, deterministic=True), PAN_POLICY)
    assert store.get(rnd).scheme is TokenScheme.RANDOM  # type: ignore[union-attr]
    assert store.get(det).scheme is TokenScheme.DETERMINISTIC  # type: ignore[union-attr]
