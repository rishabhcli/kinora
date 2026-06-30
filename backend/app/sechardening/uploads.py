"""Upload validation: magic-byte content-type checking and size enforcement.

All functions are **pure** (operating on ``bytes`` / ``int`` scalars) so they
can be called from any async or sync context without I/O coupling.  The caller
is responsible for reading the file bytes from the incoming request.

Magic-byte sniffers
-------------------
Each supported format is identified by one or more byte patterns at a fixed
offset (usually 0).  We deliberately support only the MIME types the Kinora
backend accepts so the whitelist is tight:

* ``application/pdf``  — ``%PDF-`` at offset 0
* ``image/png``        — 8-byte PNG signature
* ``image/jpeg``       — SOI marker ``\\xFF\\xD8\\xFF``
* ``image/webp``       — ``RIFF….WEBP`` (bytes 0-3 + 8-11)
* ``video/mp4``        — ``ftyp`` box at offset 4 (any ISO base-media file)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

__all__ = [
    "UploadError",
    "ContentTypeMismatchError",
    "FileTooLargeError",
    "SUPPORTED_MIME_TYPES",
    "sniff_mime_type",
    "validate_upload",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum bytes needed to identify any supported format.
_MIN_SNIFF_BYTES: Final[int] = 12

#: Default maximum upload size: 100 MiB.
DEFAULT_MAX_BYTES: Final[int] = 100 * 1_024 * 1_024

#: MIME types the upload validator recognises.
SUPPORTED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "video/mp4",
    }
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UploadError(ValueError):
    """Base class for upload-validation errors."""


class ContentTypeMismatchError(UploadError):
    """Raised when the declared MIME type does not match the magic bytes.

    Attributes:
        declared: The ``Content-Type`` the client claimed.
        detected: The MIME type inferred from magic bytes, or ``None`` if
            the bytes do not match any supported format.
    """

    def __init__(self, declared: str, detected: str | None) -> None:
        self.declared = declared
        self.detected = detected
        super().__init__(
            f"Content-type mismatch: declared={declared!r}, "
            f"detected={detected!r} from magic bytes"
        )


class FileTooLargeError(UploadError):
    """Raised when the upload exceeds the configured size limit.

    Attributes:
        size: Actual size in bytes.
        limit: The configured limit in bytes.
    """

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"Upload too large: {size} bytes > limit {limit} bytes")


# ---------------------------------------------------------------------------
# Magic-byte sniffers
# ---------------------------------------------------------------------------

# Each sniffer is a ``Callable[[bytes], bool]`` that returns True when the
# header matches the corresponding format.  Sniffers only inspect a small
# prefix so they are fast and never block on I/O.

_SNIFFERS: list[tuple[str, Callable[[bytes], bool]]] = [
    # PDF: starts with "%PDF-" (5 bytes).
    ("application/pdf", lambda h: h[:5] == b"%PDF-"),
    # PNG: 8-byte magic signature.
    ("image/png", lambda h: h[:8] == b"\x89PNG\r\n\x1a\n"),
    # JPEG: SOI marker + JFIF/Exif/raw FF.
    ("image/jpeg", lambda h: h[:3] == b"\xff\xd8\xff"),
    # WebP: RIFF at [0:4] and WEBP at [8:12].
    (
        "image/webp",
        lambda h: len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WEBP",
    ),
    # MP4 / ISO base-media: "ftyp" box type at offset 4 (after 4-byte box size).
    (
        "video/mp4",
        lambda h: len(h) >= 8 and h[4:8] == b"ftyp",
    ),
]


def sniff_mime_type(header: bytes) -> str | None:
    """Return the MIME type inferred from *header* magic bytes, or ``None``.

    Args:
        header: The leading bytes of the file (at least :data:`_MIN_SNIFF_BYTES`
            recommended; shorter inputs are tolerated but may miss some formats).

    Returns:
        A MIME-type string (e.g. ``"image/jpeg"``) or ``None`` if no known
        format matches.
    """
    for mime, sniffer in _SNIFFERS:
        if sniffer(header):
            return mime
    return None


# ---------------------------------------------------------------------------
# Public validator
# ---------------------------------------------------------------------------


def validate_upload(
    data: bytes,
    declared_content_type: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Validate an uploaded file's size and content-type consistency.

    Checks performed (in order):
    1. **Size** — ``len(data) <= max_bytes``.
    2. **Declared type** — ``declared_content_type`` must be in
       :data:`SUPPORTED_MIME_TYPES` (the bare MIME type; parameters like
       ``; charset=utf-8`` are stripped before comparison).
    3. **Magic-byte match** — the detected MIME type from magic bytes must
       equal the declared type.

    Args:
        data: The complete file bytes (already buffered by the caller).
        declared_content_type: The ``Content-Type`` header value from the
            HTTP request.  Parameters (e.g. ``; boundary=…``) are stripped.
        max_bytes: Upper bound on ``len(data)``.  Defaults to 100 MiB.

    Returns:
        The canonical MIME type string (e.g. ``"application/pdf"``).

    Raises:
        FileTooLargeError: If ``len(data) > max_bytes``.
        ContentTypeMismatchError: If the declared type is not supported, or
            if the magic-byte detection disagrees with the declared type.
    """
    # 1. Size check.
    if len(data) > max_bytes:
        raise FileTooLargeError(len(data), max_bytes)

    # 2. Strip parameters from declared type (e.g. "image/jpeg; exif=…").
    bare_declared = declared_content_type.split(";")[0].strip().lower()

    if bare_declared not in SUPPORTED_MIME_TYPES:
        raise ContentTypeMismatchError(bare_declared, None)

    # 3. Magic-byte detection.
    detected = sniff_mime_type(data[:_MIN_SNIFF_BYTES])
    if detected != bare_declared:
        raise ContentTypeMismatchError(bare_declared, detected)

    return bare_declared
