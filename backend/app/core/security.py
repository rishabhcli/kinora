"""Cryptographic security primitives (kinora.md §6, §12 — the security plane).

This module is the **pure-crypto foundation** of the auth system: no I/O, no DB,
no network — just hashing, token generation, password policy, RFC 6238 TOTP,
recovery codes, and API-key fingerprinting. Everything stateful (JWT issuance,
refresh-token families, sessions, RBAC, audit) is layered on top of these
primitives in :mod:`app.auth`.

Design notes:

* **Password hashing** goes through a pluggable :class:`PasswordHasher` so the
  algorithm (bcrypt today, argon2id when the optional dependency is present) is a
  configuration choice, not a code change. The hasher reports when a stored hash
  was made with weaker parameters than the current policy so the caller can
  transparently re-hash on the next successful login (``needs_rehash``).
* **bcrypt truncates at 72 bytes** and raises on longer inputs since ≥ 4.1, so the
  bcrypt backend pre-hashes the password with SHA-256 (base64) — this both
  removes the 72-byte ceiling *and* avoids the password-shucking class of bugs
  that a naive truncation invites.
* **Constant-time comparison** (:func:`constant_time_compare`) is used for every
  secret/digest check so token verification leaks no timing signal.
* **TOTP** is implemented straight from RFC 6238 / RFC 4226 on the stdlib
  (``hmac`` + ``hashlib``) — no extra dependency — and verified with a small
  drift window so a slightly-skewed authenticator still works.

The legacy ``app.api.security`` helpers (``hash_password`` / ``verify_password``)
are re-exported from here via a thin shim so existing imports keep working while
new code uses the richer surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from typing import Protocol

import bcrypt

# --------------------------------------------------------------------------- #
# Secure randomness + constant-time compare
# --------------------------------------------------------------------------- #

#: bcrypt only consumes the first 72 bytes; we pre-hash to lift that ceiling.
_BCRYPT_MAX_BYTES = 72
#: Default bcrypt work factor. 12 is a good 2026 balance of cost vs. latency.
DEFAULT_BCRYPT_ROUNDS = 12


def generate_token(nbytes: int = 32) -> str:
    """Return a URL-safe, cryptographically-random opaque token string.

    Used for refresh tokens, API-key secrets, password-reset tokens, CSRF
    tokens, and session ids — anywhere an unguessable bearer secret is needed.
    """
    if nbytes < 16:  # 128 bits is the floor for an unguessable bearer secret
        raise ValueError("refusing to mint a token with fewer than 16 random bytes")
    return secrets.token_urlsafe(nbytes)


def generate_numeric_code(digits: int = 6) -> str:
    """Return a uniformly-random zero-padded numeric code (e.g. an email OTP)."""
    if not 4 <= digits <= 12:
        raise ValueError("numeric code length must be between 4 and 12 digits")
    upper = 10**digits
    return str(secrets.randbelow(upper)).zfill(digits)


def constant_time_compare(a: str | bytes, b: str | bytes) -> bool:
    """Compare two secrets in constant time (timing-attack safe).

    Accepts ``str`` or ``bytes``; mixed types compare unequal. Always runs the
    full comparison regardless of an early length mismatch.
    """
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


def sha256_hex(value: str | bytes) -> str:
    """Return the hex SHA-256 digest of ``value`` (used for opaque-token lookups).

    Opaque bearer secrets (refresh tokens, API keys) are stored only as their
    SHA-256 digest so a database leak never yields a usable credential, while the
    digest is still a deterministic lookup key.
    """
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: str | bytes, value: str | bytes) -> str:
    """Return a keyed (peppered) HMAC-SHA-256 hex digest.

    API-key secrets are fingerprinted with an HMAC keyed by a server-side pepper
    so the stored digest is useless even to an attacker who exfiltrates the table
    *and* knows the plaintext-to-SHA-256 mapping but not the pepper.
    """
    key_b = key.encode("utf-8") if isinstance(key, str) else key
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key_b, data, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Pluggable password hashing
# --------------------------------------------------------------------------- #


class PasswordHasher(Protocol):
    """A swappable password-hashing scheme (bcrypt, argon2id, ...)."""

    #: A short stable identifier stored alongside callers' policy decisions.
    scheme: str

    def hash(self, password: str) -> str:
        """Return an encoded hash string safe to persist."""
        ...

    def verify(self, password: str, encoded: str) -> bool:
        """Verify ``password`` against a previously-encoded hash."""
        ...

    def needs_rehash(self, encoded: str) -> bool:
        """Whether ``encoded`` was made with weaker params than current policy."""
        ...

    def identify(self, encoded: str) -> bool:
        """Whether ``encoded`` looks like a hash this scheme produced."""
        ...


def _bcrypt_prehash(password: str) -> bytes:
    """SHA-256 + base64 a password so bcrypt sees ≤ 72 deterministic bytes.

    Lifts bcrypt's 72-byte truncation ceiling (so long passphrases keep all their
    entropy) and avoids the silent-truncation footgun.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


class BcryptHasher:
    """bcrypt password hashing (the default; ``passlib[bcrypt]`` extra is declared).

    Hashes the SHA-256+base64 pre-image of the password (see :func:`_bcrypt_prehash`)
    so inputs longer than 72 bytes neither error nor lose entropy.
    """

    scheme = "bcrypt"

    def __init__(self, rounds: int = DEFAULT_BCRYPT_ROUNDS) -> None:
        if not 4 <= rounds <= 31:
            raise ValueError("bcrypt rounds must be between 4 and 31")
        self._rounds = rounds

    def hash(self, password: str) -> str:
        salt = bcrypt.gensalt(rounds=self._rounds)
        return bcrypt.hashpw(_bcrypt_prehash(password), salt).decode("ascii")

    def verify(self, password: str, encoded: str) -> bool:
        try:
            return bcrypt.checkpw(_bcrypt_prehash(password), encoded.encode("ascii"))
        except (ValueError, TypeError):
            return False

    def needs_rehash(self, encoded: str) -> bool:
        # bcrypt encodes the cost as ``$2b$<rounds>$...``; rehash if it differs.
        try:
            cost = int(encoded.split("$")[2])
        except (IndexError, ValueError):
            return True
        return cost != self._rounds

    def identify(self, encoded: str) -> bool:
        return encoded.startswith(("$2a$", "$2b$", "$2y$"))


class Argon2Hasher:
    """argon2id hashing — used when the optional ``argon2-cffi`` dep is installed.

    Falls back is the caller's concern: :func:`build_password_hasher` only selects
    this when the import succeeds, so importing this module never hard-requires
    argon2.
    """

    scheme = "argon2"

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost: int = 64 * 1024,
        parallelism: int = 2,
    ) -> None:
        from argon2 import PasswordHasher as _Argon2  # local import: optional dep
        from argon2.exceptions import InvalidHashError, VerifyMismatchError

        self._ph = _Argon2(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)
        self._mismatch = VerifyMismatchError
        self._invalid = InvalidHashError

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, password: str, encoded: str) -> bool:
        try:
            return self._ph.verify(encoded, password)
        except (self._mismatch, self._invalid, Exception):  # noqa: BLE001
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return bool(self._ph.check_needs_rehash(encoded))
        except Exception:  # noqa: BLE001
            return True

    def identify(self, encoded: str) -> bool:
        return encoded.startswith("$argon2")


def build_password_hasher(scheme: str = "bcrypt", *, rounds: int | None = None) -> PasswordHasher:
    """Construct the configured :class:`PasswordHasher`.

    ``scheme`` is ``"bcrypt"`` (default, always available) or ``"argon2"`` (used
    when ``argon2-cffi`` is importable; otherwise falls back to bcrypt so the app
    never fails to boot for a missing optional dependency).
    """
    scheme = scheme.lower().strip()
    if scheme == "argon2":
        try:
            return Argon2Hasher()
        except Exception:  # noqa: BLE001 - argon2-cffi not installed; degrade
            return BcryptHasher(rounds or DEFAULT_BCRYPT_ROUNDS)
    return BcryptHasher(rounds or DEFAULT_BCRYPT_ROUNDS)


# --------------------------------------------------------------------------- #
# Legacy compatibility shim (app.api.security re-exports these)
# --------------------------------------------------------------------------- #

#: A process-wide default hasher for the legacy module-level helpers.
_DEFAULT_HASHER: PasswordHasher = BcryptHasher()


def hash_password(password: str) -> str:
    """Hash ``password`` with the default scheme (legacy helper)."""
    return _DEFAULT_HASHER.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify ``password`` against ``hashed`` (legacy helper)."""
    return _DEFAULT_HASHER.verify(password, hashed)


# --------------------------------------------------------------------------- #
# Password strength policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """A configurable password-strength policy (NIST-flavoured)."""

    min_length: int = 8
    max_length: int = 200
    require_lower: bool = True
    require_upper: bool = True
    require_digit: bool = True
    require_symbol: bool = False
    #: Reject the most-common weak passwords outright (small embedded denylist).
    block_common: bool = True
    #: Minimum distinct characters (defeats "aaaaaaaa" / "12121212").
    min_unique: int = 4

    def validate(self, password: str) -> list[str]:
        """Return a list of human-readable policy violations (empty == OK)."""
        problems: list[str] = []
        if len(password) < self.min_length:
            problems.append(f"must be at least {self.min_length} characters")
        if len(password) > self.max_length:
            problems.append(f"must be at most {self.max_length} characters")
        if self.require_lower and not re.search(r"[a-z]", password):
            problems.append("must contain a lowercase letter")
        if self.require_upper and not re.search(r"[A-Z]", password):
            problems.append("must contain an uppercase letter")
        if self.require_digit and not re.search(r"\d", password):
            problems.append("must contain a digit")
        if self.require_symbol and not re.search(r"[^A-Za-z0-9]", password):
            problems.append("must contain a symbol")
        if self.min_unique and len(set(password)) < self.min_unique:
            problems.append(f"must contain at least {self.min_unique} distinct characters")
        if self.block_common and password.lower() in COMMON_PASSWORDS:
            problems.append("is too common; choose a less predictable password")
        return problems

    def is_valid(self, password: str) -> bool:
        """Whether ``password`` satisfies the policy."""
        return not self.validate(password)


#: A compact denylist of the most-abused passwords (credential-stuffing fodder).
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "iloveyou1",
        "admin123",
        "letmein123",
        "welcome123",
        "monkey123",
        "abc12345",
        "football1",
        "baseball1",
        "dragon123",
        "sunshine1",
        "princess1",
        "changeme1",
        "trustno1",
        "superman1",
        "starwars1",
    }
)


def password_entropy_bits(password: str) -> float:
    """Estimate password entropy in bits (rough, charset-size * length model).

    Used only to surface an advisory "strength" score in API responses — never to
    accept/reject (that is :class:`PasswordPolicy`).
    """
    if not password:
        return 0.0
    pool = 0
    if re.search(r"[a-z]", password):
        pool += 26
    if re.search(r"[A-Z]", password):
        pool += 26
    if re.search(r"\d", password):
        pool += 10
    if re.search(r"[^A-Za-z0-9]", password):
        pool += 33
    pool = max(pool, 1)
    return round(len(password) * math.log2(pool), 2)


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238) + recovery codes
# --------------------------------------------------------------------------- #

#: RFC 3548 base32 alphabet length for secret generation (multiples of 8 chars).
_TOTP_SECRET_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def generate_totp_secret(length: int = 32) -> str:
    """Return a fresh base32 TOTP secret (the QR-encoded shared key).

    32 base32 chars == 160 bits, the RFC 4226 recommended HMAC-SHA-1 key size.
    """
    if length < 16 or length % 8 != 0:
        raise ValueError("TOTP secret length must be a multiple of 8 and >= 16")
    return "".join(secrets.choice(_TOTP_SECRET_CHARS) for _ in range(length))


def _b32decode(secret: str) -> bytes:
    """Decode a (possibly unpadded, lowercase) base32 TOTP secret to bytes."""
    cleaned = secret.strip().replace(" ", "").upper()
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad))


def totp_now(
    secret: str, *, period: int = 30, digits: int = 6, timestamp: float | None = None
) -> str:
    """Compute the current RFC 6238 TOTP code for ``secret``."""
    ts = time.time() if timestamp is None else timestamp
    counter = int(ts // period)
    return _hotp(secret, counter, digits=digits)


def _hotp(secret: str, counter: int, *, digits: int = 6) -> str:
    """RFC 4226 HOTP for an explicit counter (the engine behind TOTP)."""
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def verify_totp(
    secret: str,
    code: str,
    *,
    period: int = 30,
    digits: int = 6,
    window: int = 1,
    timestamp: float | None = None,
) -> bool:
    """Verify a TOTP ``code`` against ``secret`` with a ±``window`` step drift.

    A ``window`` of 1 accepts the previous, current, and next step (±30s) to
    tolerate clock skew between the authenticator and the server. Comparison is
    constant-time; a malformed code returns ``False`` rather than raising.
    """
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits:
        return False
    ts = time.time() if timestamp is None else timestamp
    counter = int(ts // period)
    for drift in range(-window, window + 1):
        candidate = _hotp(secret, counter + drift, digits=digits)
        if constant_time_compare(candidate, code):
            return True
    return False


def totp_provisioning_uri(secret: str, *, account: str, issuer: str, digits: int = 6) -> str:
    """Build the ``otpauth://`` URI an authenticator app encodes into a QR code."""
    from urllib.parse import quote, urlencode

    label = quote(f"{issuer}:{account}")
    params = urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": digits, "period": 30}
    )
    return f"otpauth://totp/{label}?{params}"


@dataclass(frozen=True, slots=True)
class RecoveryCode:
    """A single MFA recovery code: the plaintext (shown once) + its lookup digest."""

    plaintext: str
    digest: str


def generate_recovery_codes(
    count: int = 10, *, groups: int = 2, group_len: int = 5
) -> list[RecoveryCode]:
    """Mint ``count`` single-use recovery codes (plaintext + SHA-256 digest).

    Codes look like ``ab3cd-9xk2p`` — uppercase-insensitive, grouped for legibility.
    Only the digest is persisted; the plaintext is returned once for the user to
    store safely.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no ambiguous 0/o/1/l/i
    out: list[RecoveryCode] = []
    for _ in range(count):
        parts = ["".join(secrets.choice(alphabet) for _ in range(group_len)) for _ in range(groups)]
        plaintext = "-".join(parts)
        digest = sha256_hex(normalize_recovery_code(plaintext))
        out.append(RecoveryCode(plaintext=plaintext, digest=digest))
    return out


def normalize_recovery_code(code: str) -> str:
    """Normalise a user-entered recovery code for digest comparison."""
    return code.strip().lower().replace(" ", "")


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #

#: Human-recognisable prefix so a leaked key is identifiable (and revocable fast).
API_KEY_PREFIX = "kino_sk_"
#: How many leading chars of the secret are stored in clear for display/lookup.
API_KEY_DISPLAY_CHARS = 8


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """A freshly-minted API key: the full secret (shown once) + storage fields."""

    #: The full ``kino_sk_...`` string handed to the caller exactly once.
    secret: str
    #: A short non-secret id embedded in the key, used as the DB lookup handle.
    key_id: str
    #: A short clear prefix of the random part for display ("kino_sk_abcd…").
    display_prefix: str = ""


def generate_api_key(*, pepper: str) -> tuple[IssuedApiKey, str]:
    """Mint an API key. Returns the issued key + the peppered digest to store.

    The wire format is ``kino_sk_<key_id>_<secret>``: the ``key_id`` is a public,
    indexable handle (so verification is an O(1) lookup, not a table scan) and the
    ``secret`` is the high-entropy part. Only the HMAC digest of the secret is
    persisted (keyed by ``pepper``); the plaintext is unrecoverable afterwards.
    """
    key_id = secrets.token_hex(6)
    secret_part = secrets.token_urlsafe(32)
    full = f"{API_KEY_PREFIX}{key_id}_{secret_part}"
    digest = hmac_sha256_hex(pepper, secret_part)
    display = secret_part[:API_KEY_DISPLAY_CHARS]
    return IssuedApiKey(secret=full, key_id=key_id, display_prefix=display), digest


def parse_api_key(presented: str) -> tuple[str, str] | None:
    """Split a presented API key into ``(key_id, secret_part)`` or ``None``.

    Returns ``None`` for anything not matching the ``kino_sk_<id>_<secret>`` shape
    so a malformed header is rejected without a DB round-trip.
    """
    if not presented.startswith(API_KEY_PREFIX):
        return None
    body = presented[len(API_KEY_PREFIX) :]
    key_id, _, secret_part = body.partition("_")
    if not key_id or not secret_part:
        return None
    return key_id, secret_part


def verify_api_key(secret_part: str, stored_digest: str, *, pepper: str) -> bool:
    """Constant-time check of a presented API-key secret against its stored digest."""
    return constant_time_compare(hmac_sha256_hex(pepper, secret_part), stored_digest)


# --------------------------------------------------------------------------- #
# Device / user-agent fingerprinting (best-effort, for session display)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DeviceInfo:
    """A coarse, privacy-respecting description of a login device."""

    user_agent: str | None = None
    ip: str | None = None
    platform: str | None = None
    browser: str | None = None
    fingerprint: str = field(default="")

    @property
    def label(self) -> str:
        """A short human label for the sessions list ("Chrome on macOS")."""
        if self.browser and self.platform:
            return f"{self.browser} on {self.platform}"
        return self.browser or self.platform or "Unknown device"


_PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("Linux", "Linux"),
    ("CrOS", "ChromeOS"),
)
_BROWSER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Safari/", "Safari"),
    ("Electron/", "Kinora Desktop"),
)


def parse_device(user_agent: str | None, ip: str | None = None) -> DeviceInfo:
    """Parse a coarse :class:`DeviceInfo` from a User-Agent header (best-effort)."""
    info = DeviceInfo(user_agent=user_agent, ip=ip)
    if user_agent:
        for needle, name in _PLATFORM_PATTERNS:
            if needle in user_agent:
                info.platform = name
                break
        for needle, name in _BROWSER_PATTERNS:
            if needle in user_agent:
                info.browser = name
                break
    seed = f"{user_agent or ''}|{ip or ''}"
    info.fingerprint = sha256_hex(seed)[:32]
    return info


__all__ = [
    "API_KEY_DISPLAY_CHARS",
    "API_KEY_PREFIX",
    "COMMON_PASSWORDS",
    "DEFAULT_BCRYPT_ROUNDS",
    "Argon2Hasher",
    "BcryptHasher",
    "DeviceInfo",
    "IssuedApiKey",
    "PasswordHasher",
    "PasswordPolicy",
    "RecoveryCode",
    "build_password_hasher",
    "constant_time_compare",
    "generate_api_key",
    "generate_numeric_code",
    "generate_recovery_codes",
    "generate_token",
    "generate_totp_secret",
    "hash_password",
    "hmac_sha256_hex",
    "normalize_recovery_code",
    "parse_api_key",
    "parse_device",
    "password_entropy_bits",
    "sha256_hex",
    "totp_now",
    "totp_provisioning_uri",
    "verify_api_key",
    "verify_password",
    "verify_totp",
]
