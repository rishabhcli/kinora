"""Object-key / filename normalization and path-traversal defenses.

All functions are **pure** (no I/O, no network): they operate on strings and
return either a canonical storage key or raise :exc:`KeyValidationError` with a
human-readable reason.

Design rationale
----------------
Object-storage keys look like POSIX paths but are arbitrary byte strings.
Malicious or accidentally malformed keys can:

* Escape a logical "prefix" (``../../etc/passwd``)
* Embed null bytes / control chars that bypass downstream validation
* Use non-ASCII that normalizes differently across platforms
* Grow large enough to exceed S3/MinIO key limits (1 024 bytes)

This module closes each of those attack surfaces deterministically so callers
only ever persist keys they know are safe.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "KeyValidationError",
    "normalize_key",
    "is_safe_key",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: S3 / MinIO hard limit for an object key (UTF-8 bytes).  We enforce the same
#: limit after normalization so keys produced here are always storable.
MAX_KEY_BYTES: int = 1_024

#: Maximum *character* length we allow before even attempting to normalize.
#: This guards against CPU-expensive NFC normalization of huge inputs.
MAX_RAW_CHARS: int = 2_048

#: Allowed charset after normalization: ASCII alphanumerics, ``-``, ``_``,
#: ``.``, and ``/`` (for hierarchical keys).  Everything else is rejected.
_SAFE_CHAR_RE = re.compile(r"^[A-Za-z0-9\-_./]+$")

#: Segment pattern — each path component must match this after collapsing
#: separators.  An empty segment (double-slash) is banned implicitly because
#: splitting on ``/`` and filtering empty strings removes them.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9\-_.]+$")

#: Control characters including NUL (U+0000 – U+001F) and DEL (U+007F).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KeyValidationError(ValueError):
    """Raised when a candidate object key fails a safety check.

    Attributes:
        reason: Short machine-readable label for the failure class (e.g.
            ``"traversal"``, ``"control_chars"``, ``"unsafe_charset"``).
        raw: The original input that failed validation.
    """

    def __init__(self, reason: str, raw: str) -> None:
        self.reason = reason
        self.raw = raw
        super().__init__(f"Key validation failed [{reason}]: {raw!r}")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def normalize_key(raw: str, *, prefix: str = "") -> str:
    """Return a canonical, safe storage key derived from *raw*.

    Processing pipeline
    -------------------
    1. Guard against absurdly large inputs before any Unicode work.
    2. Strip leading/trailing whitespace.
    3. NFC-normalize so the same logical string always maps to one key.
    4. Reject any NUL or ASCII control character.
    5. Reject absolute paths (leading ``/``) and the naked ``..`` / ``.``
       traversal components, including URL-encoded variants (``%2e``).
    6. Collapse repeated ``/`` separators; strip leading and trailing ``/``.
    7. Validate every path segment against :data:`_SEGMENT_RE`.
    8. Enforce the allowed charset over the assembled key.
    9. Prepend *prefix* (if given) and verify the final UTF-8 byte count fits
       within :data:`MAX_KEY_BYTES`.

    Args:
        raw: The candidate filename or object-key string supplied by the user
            or an upstream component.
        prefix: Optional storage prefix (e.g. ``"books/"`` or ``"media/"``).
            Must itself be safe; it is **not** validated here — only the
            *combined* length is checked.

    Returns:
        A canonical storage key that is safe to pass to S3/MinIO.

    Raises:
        KeyValidationError: If any safety check fails.
    """
    if len(raw) > MAX_RAW_CHARS:
        raise KeyValidationError("too_long", raw)

    candidate = raw.strip()

    # NFC normalization (``é`` as one codepoint, not combining accent).
    candidate = unicodedata.normalize("NFC", candidate)

    # Reject control characters (including NUL).
    if _CONTROL_RE.search(candidate):
        raise KeyValidationError("control_chars", raw)

    # Reject URL-encoded dot-segments before any path splitting.
    if re.search(r"%2e", candidate, re.IGNORECASE):
        raise KeyValidationError("traversal", raw)

    # Reject absolute paths.
    if candidate.startswith("/"):
        raise KeyValidationError("absolute_path", raw)

    # Collapse repeated separators and strip surrounding slashes.
    candidate = re.sub(r"/+", "/", candidate).strip("/")

    if not candidate:
        raise KeyValidationError("empty", raw)

    # Check each segment for traversal components and safe charset.
    segments = candidate.split("/")
    for seg in segments:
        if seg in (".", ".."):
            raise KeyValidationError("traversal", raw)
        if not _SEGMENT_RE.match(seg):
            raise KeyValidationError("unsafe_segment", raw)

    # Full-key charset check (redundant with segment check but explicit).
    if not _SAFE_CHAR_RE.match(candidate):
        raise KeyValidationError("unsafe_charset", raw)

    key = (prefix + candidate) if prefix else candidate

    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise KeyValidationError("key_too_long", raw)

    return key


def is_safe_key(raw: str, *, prefix: str = "") -> bool:
    """Return ``True`` if *raw* normalizes to a valid key, ``False`` otherwise.

    Convenience wrapper around :func:`normalize_key` for callers that prefer a
    boolean predicate over exception handling.
    """
    try:
        normalize_key(raw, prefix=prefix)
        return True
    except KeyValidationError:
        return False
