"""Content hashing & content-addressed key derivation (kinora.md §8.7).

§8.7 dedups at the **shot-hash** level ("memory means we never re-render a shot
that already passed QA"). This module dedups one level lower — at the **byte**
level — so two assets with identical bytes (an identical Ken-Burns card, a
re-used cover, the same poster derived twice) share a single stored blob.

The address of a blob is a function of its bytes alone:

    media/by-hash/<aa>/<bb>/<sha256><ext>

The two-nibble fan-out (``aa``/``bb``) keeps any single object-store "directory"
prefix from accumulating millions of keys, which matters for listing/GC on real
S3/OSS. Everything here is pure and streaming-friendly (no full-buffer needed for
large files) so it works for both small derived stills and large source PDFs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import BinaryIO

#: Read chunk for streaming hashes — 1 MiB balances syscalls vs memory.
_CHUNK = 1024 * 1024

#: The fixed prefix under which content-addressed blobs live.
CONTENT_ADDRESS_PREFIX = "media/by-hash"


def sha256_hex(data: bytes) -> str:
    """Return the hex sha256 of ``data`` (the canonical content digest)."""
    return hashlib.sha256(data).hexdigest()


#: Backwards-friendly alias — some callers prefer the ``_bytes`` spelling.
sha256_bytes = sha256_hex


def sha256_stream(fp: BinaryIO, *, chunk: int = _CHUNK) -> str:
    """Stream a file-like object through sha256 without buffering it whole.

    The cursor is advanced to EOF; callers that need to re-read should
    ``fp.seek(0)`` afterwards.
    """
    h = hashlib.sha256()
    while True:
        block = fp.read(chunk)
        if not block:
            break
        h.update(block)
    return h.hexdigest()


def sha256_chunks(chunks: Iterable[bytes]) -> str:
    """Hash an iterable of byte chunks (e.g. multipart parts) in order."""
    h = hashlib.sha256()
    for block in chunks:
        h.update(block)
    return h.hexdigest()


def short_digest(digest: str, *, length: int = 12) -> str:
    """A short, human-friendly prefix of a hex digest (for logs / ids)."""
    return digest[:length]


def content_address_key(digest: str, *, suffix: str = "", prefix: str | None = None) -> str:
    """Derive the deterministic object-store key for a content digest.

    ``suffix`` is an extension like ``.mp4`` / ``.png`` (leading dot optional);
    it is preserved verbatim so content-type sniffing on the edge still works.
    A custom ``prefix`` overrides :data:`CONTENT_ADDRESS_PREFIX` (e.g. to keep a
    book's content-addressed blobs under its own namespace).

    Raises:
        ValueError: if ``digest`` is not a 64-char lowercase hex sha256.
    """
    norm = digest.strip().lower()
    if len(norm) != 64 or any(c not in "0123456789abcdef" for c in norm):
        raise ValueError(f"not a sha256 hex digest: {digest!r}")
    ext = suffix if (suffix.startswith(".") or not suffix) else f".{suffix}"
    base = (prefix or CONTENT_ADDRESS_PREFIX).rstrip("/")
    return f"{base}/{norm[:2]}/{norm[2:4]}/{norm}{ext}"


def digest_from_key(key: str) -> str | None:
    """Recover the sha256 from a content-address key, or ``None`` if not one.

    Inverse of :func:`content_address_key`; tolerates a custom prefix by reading
    the last path segment (the filename is ``<sha256><ext>``).
    """
    name = key.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return None


__all__ = [
    "CONTENT_ADDRESS_PREFIX",
    "content_address_key",
    "digest_from_key",
    "sha256_bytes",
    "sha256_chunks",
    "sha256_hex",
    "sha256_stream",
    "short_digest",
]
