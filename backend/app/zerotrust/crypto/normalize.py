"""Canonicalisation for searchable-encryption inputs.

Equality search and blind indexes only match on *byte-identical* normalised
input, so the normaliser is a correctness-critical, deterministic function: the
same logical value must always produce the same bytes, at write time and at query
time, forever. Normalisers therefore live here as named, versioned, pure
functions rather than being inlined at call sites.

``casefold`` is used (not ``lower``) for case-insensitive matching because it is
the Unicode-correct full case fold; whitespace is stripped at the ends. Email
local-parts are left case-sensitive per RFC 5321 but the domain is lowercased,
which matches how virtually every provider actually treats addresses.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

#: A normaliser turns a logical value into the canonical bytes that are hashed
#: (blind index) or deterministically encrypted.
Normalizer = Callable[[str], bytes]


def identity(value: str) -> bytes:
    """No transform beyond UTF-8 encoding (use for case-sensitive identifiers)."""
    return value.encode("utf-8")


def casefold(value: str) -> bytes:
    """Trim + NFKC + Unicode case-fold — the default for human text equality."""
    return unicodedata.normalize("NFKC", value.strip()).casefold().encode("utf-8")


def email(value: str) -> bytes:
    """Normalise an email: trim, NFKC, lowercase the domain, keep the local part.

    Falls back to :func:`casefold` for inputs without exactly one ``@`` so a
    malformed value still normalises deterministically rather than raising.
    """
    trimmed = unicodedata.normalize("NFKC", value.strip())
    local, sep, domain = trimmed.partition("@")
    if not sep or "@" in domain:
        return casefold(value)
    return f"{local}@{domain.casefold()}".encode()


def digits(value: str) -> bytes:
    """Keep only ASCII digits (phone numbers, national ids with separators)."""
    return "".join(ch for ch in value if ch.isascii() and ch.isdigit()).encode("ascii")


#: A registry so a column spec can name its normaliser as a stable string (which
#: is what gets persisted in the column metadata / migration, not a function ref).
REGISTRY: dict[str, Normalizer] = {
    "identity": identity,
    "casefold": casefold,
    "email": email,
    "digits": digits,
}


def resolve(name: str) -> Normalizer:
    """Look up a normaliser by its registry name."""
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown normaliser {name!r}; known: {sorted(REGISTRY)}") from exc


__all__ = [
    "REGISTRY",
    "Normalizer",
    "casefold",
    "digits",
    "email",
    "identity",
    "resolve",
]
