"""Format-preserving tokenization vault for PII.

Tokenization replaces a sensitive value (a card number, an SSN, an email) with a
surrogate *token* that has **no mathematical relationship** to the original — the
mapping lives only in the vault. This differs from encryption: there is no key
that turns a token back into plaintext, only a vault lookup gated by
authorisation. It is the right tool when downstream systems must keep handling a
value's *shape* (a 16-digit string stays 16 digits) but must never see the real
data, and when you want a hard "detokenize is an audited, authorised event".

Format preservation
-------------------
:class:`FormatSpec` describes the alphabet and length to preserve. Tokens are
drawn uniformly from that format's space using the OS CSPRNG, with collision
re-draw against the vault, so a token is type-compatible with the column it
replaces (passes the same length/charset validation as the original). A small
keyed luhn-free check is *not* added — tokens are opaque surrogates, validated by
shape only.

Determinism (optional)
----------------------
``FormatSpec.deterministic`` makes the *same plaintext map to the same token*
(so a value tokenized twice is one vault row and joins/dedup still work),
implemented via a keyed PRF over the plaintext expanded into the format space. By
default tokens are random (a fresh token per occurrence — maximal unlinkability).

Authorisation
-------------
:meth:`TokenizationVault.detokenize` requires a :class:`DetokenizationRequest`
carrying the caller's identity and a *purpose*; the token's :class:`TokenPolicy`
declares which purposes may reveal it. Unauthorised detokenization raises
:class:`~app.zerotrust.crypto.errors.AuthorizationError` and is recorded by the
vault's audit hook. The stored value is itself AEAD-encrypted at rest (envelope
hierarchy), so the vault store leaking is not a plaintext breach.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.zerotrust.crypto.context import AssociatedData, Ciphertext, CryptoProvider
from app.zerotrust.crypto.errors import (
    AuthorizationError,
    TokenizationError,
)

#: How many times to re-draw a random token on a vault collision before giving up
#: (a tiny format space that is nearly exhausted). 16 is generous; exhaustion of a
#: realistically sized format space is astronomically unlikely.
_MAX_COLLISION_REDRAWS = 16


class Alphabet(enum.Enum):
    """The character set a format-preserving token is drawn from."""

    DIGITS = "0123456789"
    HEX_LOWER = "0123456789abcdef"
    ALNUM_UPPER = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ALNUM = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """The shape a token must preserve.

    Attributes:
        alphabet: the character set tokens are drawn from.
        length: the token length in characters (matches the source value).
        prefix: literal characters copied verbatim to the token's front (e.g. a
            BIN you intentionally keep), not part of the random body.
        suffix: literal trailing characters (e.g. the last 4 of a card, kept for
            display). Prefix+suffix are *not secret*; only the body is randomised.
        deterministic: if True the same plaintext maps to the same token.
    """

    alphabet: Alphabet = Alphabet.DIGITS
    length: int = 16
    prefix: str = ""
    suffix: str = ""
    deterministic: bool = False

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise TokenizationError("format length must be positive")
        if len(self.prefix) + len(self.suffix) >= self.length:
            raise TokenizationError("prefix+suffix leave no room for a token body")

    @property
    def body_len(self) -> int:
        """The number of randomised characters (length minus literals)."""
        return self.length - len(self.prefix) - len(self.suffix)


class TokenScheme(enum.Enum):
    """Whether a token is randomly drawn or a keyed PRF of the plaintext."""

    RANDOM = "random"
    DETERMINISTIC = "deterministic"


@dataclass(frozen=True, slots=True)
class TokenPolicy:
    """Authorisation policy attached to a tokenized value.

    Attributes:
        purposes: the set of purpose strings allowed to detokenize. An empty set
            means *no one* may detokenize (write-only PII — store-and-forget).
        data_class: a label for audit/retention (e.g. ``"pan"``, ``"ssn"``).
    """

    purposes: frozenset[str] = frozenset()
    data_class: str = "pii"

    def permits(self, purpose: str) -> bool:
        return purpose in self.purposes


@dataclass(frozen=True, slots=True)
class DetokenizationRequest:
    """A request to reveal a token's plaintext, carrying the authz inputs."""

    actor: str
    purpose: str


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """A vault row: the token, the encrypted plaintext, and its policy.

    ``ciphertext`` is the AEAD envelope + wrapped DEK protecting the plaintext at
    rest, so the vault store is not a plaintext datastore even before authz.
    """

    token: str
    ciphertext: Ciphertext
    policy: TokenPolicy
    scheme: TokenScheme


class TokenStore(Protocol):
    """Persistence seam for the vault (in-memory default; a DB repo in prod)."""

    def put(self, record: TokenRecord) -> None: ...

    def get(self, token: str) -> TokenRecord | None: ...

    def get_by_plaintext_id(self, plaintext_id: str) -> TokenRecord | None: ...

    def exists(self, token: str) -> bool: ...


@dataclass
class InMemoryTokenStore:
    """A dict-backed :class:`TokenStore` for tests and single-process use."""

    _by_token: dict[str, TokenRecord] = field(default_factory=dict)
    _by_plaintext: dict[str, TokenRecord] = field(default_factory=dict)

    def put(self, record: TokenRecord) -> None:
        self._by_token[record.token] = record

    def get(self, token: str) -> TokenRecord | None:
        return self._by_token.get(token)

    def get_by_plaintext_id(self, plaintext_id: str) -> TokenRecord | None:
        return self._by_plaintext.get(plaintext_id)

    def index_plaintext(self, plaintext_id: str, record: TokenRecord) -> None:
        self._by_plaintext[plaintext_id] = record

    def exists(self, token: str) -> bool:
        return token in self._by_token


#: A hook called on every detokenize attempt (allowed or denied) for audit.
AuditHook = Callable[["DetokenizationRequest", str, bool], None]


class TokenizationVault:
    """Tokenize PII into format-preserving surrogates; detokenize under authz."""

    def __init__(
        self,
        provider: CryptoProvider,
        store: TokenStore,
        *,
        prf_key_purpose: bytes = b"tokenization-prf",
        kek_id: str | None = None,
        audit_hook: AuditHook | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._kek_id = kek_id or provider.kek_id
        self._audit = audit_hook
        # Keyed PRF secret for deterministic tokenization (column-stable, KEK-bound).
        self._prf_key = provider.column_search_seed(self._kek_id)
        self._prf_key = hmac.new(self._prf_key, prf_key_purpose, hashlib.sha256).digest()

    # -- tokenize ------------------------------------------------------------ #

    def tokenize(
        self,
        plaintext: str,
        fmt: FormatSpec,
        policy: TokenPolicy,
    ) -> str:
        """Replace ``plaintext`` with a format-preserving token; persist the mapping.

        For a deterministic format, a repeat of the same plaintext returns the
        existing token (one vault row). For a random format every call mints a
        fresh token. The plaintext is AEAD-encrypted at rest with the *token* bound
        as associated data, so a vault row cannot be relocated under another token.
        """
        scheme = (
            TokenScheme.DETERMINISTIC if fmt.deterministic else TokenScheme.RANDOM
        )
        if scheme is TokenScheme.DETERMINISTIC:
            existing = self._store.get_by_plaintext_id(self._plaintext_id(plaintext, fmt))
            if existing is not None:
                return existing.token
            token = self._derive_token(plaintext, fmt)
        else:
            token = self._fresh_token(fmt)

        aad = AssociatedData(table="token_vault", column="plaintext", record_id=token)
        ciphertext = self._provider.encrypt(
            plaintext.encode("utf-8"), aad, kek_id=self._kek_id
        )
        record = TokenRecord(
            token=token, ciphertext=ciphertext, policy=policy, scheme=scheme
        )
        self._store.put(record)
        if scheme is TokenScheme.DETERMINISTIC and isinstance(self._store, InMemoryTokenStore):
            self._store.index_plaintext(self._plaintext_id(plaintext, fmt), record)
        return token

    # -- detokenize (authorised) -------------------------------------------- #

    def detokenize(self, token: str, request: DetokenizationRequest) -> str:
        """Reveal the plaintext behind ``token`` if ``request`` is authorised.

        Raises:
            TokenizationError: unknown token.
            AuthorizationError: the token's policy does not permit the request's
                purpose. The attempt is audited either way.
        """
        record = self._store.get(token)
        if record is None:
            self._emit_audit(request, token, allowed=False)
            raise TokenizationError("unknown token")
        if not record.policy.permits(request.purpose):
            self._emit_audit(request, token, allowed=False)
            raise AuthorizationError(
                f"actor {request.actor!r} not permitted to detokenize for "
                f"purpose {request.purpose!r}"
            )
        # record_id is recoverable from the AAD-bound ciphertext path; we stored a
        # stable record id equal to the token for the vault's own AAD.
        aad = AssociatedData(
            table="token_vault", column="plaintext", record_id=self._aad_id(record)
        )
        plaintext = self._provider.decrypt(record.ciphertext, aad)
        self._emit_audit(request, token, allowed=True)
        return plaintext.decode("utf-8")

    def peek_policy(self, token: str) -> TokenPolicy | None:
        """Return a token's policy without revealing plaintext (for UI gating)."""
        record = self._store.get(token)
        return record.policy if record else None

    # -- token construction -------------------------------------------------- #

    def _fresh_token(self, fmt: FormatSpec) -> str:
        for _ in range(_MAX_COLLISION_REDRAWS):
            token = self._assemble(fmt, self._random_body(fmt))
            if not self._store.exists(token):
                return token
        raise TokenizationError(
            "could not mint a non-colliding token; format space too small"
        )

    def _derive_token(self, plaintext: str, fmt: FormatSpec) -> str:
        body = self._prf_body(plaintext, fmt)
        return self._assemble(fmt, body)

    def _assemble(self, fmt: FormatSpec, body: str) -> str:
        return f"{fmt.prefix}{body}{fmt.suffix}"

    def _random_body(self, fmt: FormatSpec) -> str:
        alphabet = fmt.alphabet.value
        n = len(alphabet)
        return "".join(alphabet[b % n] for b in os.urandom(fmt.body_len))

    def _prf_body(self, plaintext: str, fmt: FormatSpec) -> str:
        """Expand a keyed HMAC of the plaintext into the format alphabet."""
        alphabet = fmt.alphabet.value
        n = len(alphabet)
        out: list[str] = []
        counter = 0
        seed = self._plaintext_id(plaintext, fmt).encode("ascii")
        while len(out) < fmt.body_len:
            block = hmac.new(
                self._prf_key, seed + counter.to_bytes(4, "big"), hashlib.sha256
            ).digest()
            for byte in block:
                if len(out) >= fmt.body_len:
                    break
                out.append(alphabet[byte % n])
            counter += 1
        return "".join(out)

    def _plaintext_id(self, plaintext: str, fmt: FormatSpec) -> str:
        """A stable, keyed id for a plaintext+format (the dedup/lookup key)."""
        material = f"{fmt.alphabet.name}:{fmt.length}:{plaintext}".encode()
        return hmac.new(self._prf_key, material, hashlib.sha256).hexdigest()

    @staticmethod
    def _aad_id(record: TokenRecord) -> str:
        # The vault binds AAD to the token string itself, which is unique and
        # stable, so a vault row cannot be relocated under another token.
        return record.token

    def _emit_audit(
        self, request: DetokenizationRequest, token: str, *, allowed: bool
    ) -> None:
        if self._audit is not None:
            self._audit(request, token, allowed)


__all__ = [
    "Alphabet",
    "AuditHook",
    "DetokenizationRequest",
    "FormatSpec",
    "InMemoryTokenStore",
    "TokenizationVault",
    "TokenPolicy",
    "TokenRecord",
    "TokenScheme",
    "TokenStore",
]
