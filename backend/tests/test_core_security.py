"""Unit tests for the pure crypto primitives in :mod:`app.core.security` (§6/§12).

No infrastructure: every function here is pure, so this suite runs anywhere.
"""

from __future__ import annotations

import base64
import time

import pytest

from app.core import security as sec

# --------------------------------------------------------------------------- #
# Password hashing (pluggable hasher)
# --------------------------------------------------------------------------- #


def test_bcrypt_hash_roundtrip() -> None:
    hasher = sec.BcryptHasher(rounds=4)  # low rounds = fast test
    encoded = hasher.hash("correct horse battery staple")
    assert hasher.identify(encoded)
    assert hasher.verify("correct horse battery staple", encoded)
    assert not hasher.verify("wrong password", encoded)


def test_bcrypt_long_password_not_truncated() -> None:
    """Two 72+ byte passwords differing only past byte 72 must NOT collide.

    The SHA-256 pre-hash is what removes bcrypt's 72-byte truncation footgun.
    """
    hasher = sec.BcryptHasher(rounds=4)
    base = "x" * 72
    h = hasher.hash(base + "AAAA")
    assert not hasher.verify(base + "BBBB", h)
    assert hasher.verify(base + "AAAA", h)


def test_bcrypt_needs_rehash_on_cost_change() -> None:
    weak = sec.BcryptHasher(rounds=4).hash("hunter2hunter")
    assert sec.BcryptHasher(rounds=6).needs_rehash(weak)
    assert not sec.BcryptHasher(rounds=4).needs_rehash(weak)


def test_build_password_hasher_defaults_to_bcrypt() -> None:
    assert sec.build_password_hasher("bcrypt").scheme == "bcrypt"
    # argon2 falls back to bcrypt when the optional dep is missing — never crashes.
    h = sec.build_password_hasher("argon2")
    assert h.scheme in {"argon2", "bcrypt"}


def test_legacy_helpers_still_work() -> None:
    encoded = sec.hash_password("a-decent-password-1")
    assert sec.verify_password("a-decent-password-1", encoded)
    assert not sec.verify_password("nope", encoded)


def test_verify_password_rejects_garbage_hash() -> None:
    assert not sec.verify_password("anything", "not-a-real-hash")


# --------------------------------------------------------------------------- #
# Tokens / digests / constant-time
# --------------------------------------------------------------------------- #


def test_generate_token_is_unguessable_and_unique() -> None:
    a, b = sec.generate_token(), sec.generate_token()
    assert a != b
    assert len(a) >= 32


def test_generate_token_rejects_short_request() -> None:
    with pytest.raises(ValueError):
        sec.generate_token(8)


def test_constant_time_compare() -> None:
    assert sec.constant_time_compare("abc", "abc")
    assert not sec.constant_time_compare("abc", "abd")
    assert sec.constant_time_compare(b"\x01\x02", b"\x01\x02")
    # str is UTF-8 encoded before comparison, so "abc" and b"abc" are equal.
    assert sec.constant_time_compare("abc", b"abc")


def test_sha256_and_hmac_are_deterministic_and_keyed() -> None:
    assert sec.sha256_hex("x") == sec.sha256_hex("x")
    assert sec.hmac_sha256_hex("k1", "x") != sec.hmac_sha256_hex("k2", "x")
    assert sec.hmac_sha256_hex("k1", "x") == sec.hmac_sha256_hex("k1", "x")


# --------------------------------------------------------------------------- #
# Password policy
# --------------------------------------------------------------------------- #


def test_password_policy_accepts_strong_and_rejects_weak() -> None:
    policy = sec.PasswordPolicy()
    assert policy.is_valid("Str0ngPass!")
    problems = policy.validate("short")
    assert problems
    assert any("at least" in p for p in problems)


def test_password_policy_blocks_common() -> None:
    policy = sec.PasswordPolicy()
    assert not policy.is_valid("password123")


def test_password_policy_requires_charsets() -> None:
    policy = sec.PasswordPolicy(require_symbol=True)
    assert policy.validate("alllower123")  # missing upper + symbol
    assert policy.is_valid("Allgood123!")


def test_password_policy_min_unique() -> None:
    policy = sec.PasswordPolicy(min_unique=5, require_upper=False, require_digit=False)
    assert not policy.is_valid("aaaaaaaa")


def test_password_entropy_increases_with_charset() -> None:
    assert sec.password_entropy_bits("aaaaaaaa") < sec.password_entropy_bits("aA1!aA1!")
    assert sec.password_entropy_bits("") == 0.0


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238)
# --------------------------------------------------------------------------- #


def test_totp_known_vector() -> None:
    """RFC 6238 appendix B vector: secret '12345678901234567890' at T=59 -> 287082."""
    secret = base64.b32encode(b"12345678901234567890").decode("ascii")
    assert sec.totp_now(secret, timestamp=59, digits=6) == "287082"


def test_totp_roundtrip_and_drift_window() -> None:
    secret = sec.generate_totp_secret()
    now = time.time()
    code = sec.totp_now(secret, timestamp=now)
    assert sec.verify_totp(secret, code, timestamp=now)
    # A code from the previous 30s step verifies within window=1.
    prev = sec.totp_now(secret, timestamp=now - 30)
    assert sec.verify_totp(secret, prev, timestamp=now, window=1)
    # ...but not with window=0.
    assert not sec.verify_totp(secret, prev, timestamp=now, window=0) or prev == code


def test_verify_totp_rejects_malformed() -> None:
    secret = sec.generate_totp_secret()
    assert not sec.verify_totp(secret, "abc")
    assert not sec.verify_totp(secret, "12345")  # wrong length


def test_totp_provisioning_uri() -> None:
    uri = sec.totp_provisioning_uri("ABCDEF", account="a@b.com", issuer="Kinora")
    assert uri.startswith("otpauth://totp/")
    assert "secret=ABCDEF" in uri
    assert "issuer=Kinora" in uri


def test_generate_totp_secret_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        sec.generate_totp_secret(7)


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #


def test_recovery_codes_unique_with_digests() -> None:
    codes = sec.generate_recovery_codes(8)
    assert len(codes) == 8
    plaintexts = {c.plaintext for c in codes}
    assert len(plaintexts) == 8
    for c in codes:
        assert c.digest == sec.sha256_hex(sec.normalize_recovery_code(c.plaintext))


def test_recovery_code_normalization_is_case_insensitive() -> None:
    assert sec.normalize_recovery_code(" AB3CD-9XK2P ") == "ab3cd-9xk2p"


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


def test_api_key_issue_parse_verify() -> None:
    pepper = "server-side-pepper"
    issued, digest = sec.generate_api_key(pepper=pepper)
    assert issued.secret.startswith(sec.API_KEY_PREFIX)
    parsed = sec.parse_api_key(issued.secret)
    assert parsed is not None
    key_id, secret_part = parsed
    assert key_id == issued.key_id
    assert sec.verify_api_key(secret_part, digest, pepper=pepper)
    assert not sec.verify_api_key(secret_part, digest, pepper="other-pepper")
    assert not sec.verify_api_key("tampered", digest, pepper=pepper)


def test_parse_api_key_rejects_malformed() -> None:
    assert sec.parse_api_key("not-a-key") is None
    assert sec.parse_api_key(sec.API_KEY_PREFIX + "missingsecret") is None


# --------------------------------------------------------------------------- #
# Device parsing
# --------------------------------------------------------------------------- #


def test_parse_device_extracts_platform_and_browser() -> None:
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    info = sec.parse_device(ua, ip="1.2.3.4")
    assert info.platform == "macOS"
    assert info.browser == "Chrome"
    assert info.label == "Chrome on macOS"
    assert len(info.fingerprint) == 32


def test_parse_device_handles_missing_ua() -> None:
    info = sec.parse_device(None)
    assert info.label == "Unknown device"
