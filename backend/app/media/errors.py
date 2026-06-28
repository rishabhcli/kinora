"""Typed errors for the media subsystem.

A small, flat hierarchy under :class:`MediaError` so callers can catch the whole
subsystem (``except MediaError``) or a specific failure (``except
ChecksumMismatchError``). Kept dependency-free so every other module can import
it without cycles.
"""

from __future__ import annotations


class MediaError(RuntimeError):
    """Base class for every error raised by :mod:`app.media`."""


class ChecksumMismatchError(MediaError):
    """A stored/downloaded blob's sha256 did not match the expected digest.

    Raised by checksum-verified reads and by the lifecycle integrity sweep when
    object-store bytes have drifted from the recorded ``content_hash``.
    """

    def __init__(self, key: str, expected: str, actual: str) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"checksum mismatch for {key!r}: expected {expected[:12]}…, got {actual[:12]}…"
        )


class MultipartError(MediaError):
    """A multipart / resumable upload was used incorrectly (bad part, ordering)."""


class UploadNotFoundError(MultipartError):
    """A multipart upload id was referenced after it completed or was aborted."""

    def __init__(self, upload_id: str) -> None:
        self.upload_id = upload_id
        super().__init__(f"no in-progress multipart upload {upload_id!r}")


class PackagingError(MediaError):
    """HLS/DASH packaging or ffmpeg derivation failed."""


class RetentionError(MediaError):
    """A retention / lifecycle policy was misconfigured or violated."""


__all__ = [
    "ChecksumMismatchError",
    "MediaError",
    "MultipartError",
    "PackagingError",
    "RetentionError",
    "UploadNotFoundError",
]
