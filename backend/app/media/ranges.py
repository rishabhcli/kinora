"""HTTP byte-range parsing + slicing (pure).

Progressive video players issue ``Range: bytes=...`` requests to seek without
downloading a whole film. When media is served by the public CDN edge this is
handled there, but for the signed/private fallback (and for any in-process media
proxy) the API needs to honour a single-range request. This module is the pure,
fully-tested core: parse the header into a concrete ``[start, end]`` against a
known length, slice bytes, and format the ``Content-Range`` response header.

Only a *single* range is supported (the common video case); a multi-range header
is rejected so the caller can fall back to a 200 full-body response — which is a
valid HTTP behaviour and keeps the surface small.
"""

from __future__ import annotations

from dataclasses import dataclass


class RangeNotSatisfiableError(ValueError):
    """The requested range cannot be satisfied against the resource length."""


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A concrete, inclusive byte range resolved against a known length."""

    start: int
    end: int  # inclusive
    total: int

    @property
    def length(self) -> int:
        """Number of bytes covered (inclusive)."""
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        """The ``Content-Range`` header value for a 206 response."""
        return f"bytes {self.start}-{self.end}/{self.total}"

    def slice(self, data: bytes) -> bytes:
        """Return the covered slice of ``data`` (end is inclusive)."""
        return data[self.start : self.end + 1]


def parse_range(header: str | None, total: int) -> ByteRange | None:
    """Parse a single-range ``Range`` header against a resource of ``total`` bytes.

    Returns ``None`` when the header is absent or not a satisfiable single
    ``bytes=`` range (the caller then serves the full body with 200). Raises
    :class:`RangeNotSatisfiableError` when the unit is ``bytes`` and the range is
    syntactically valid but out of bounds (the caller returns 416).

    Supported forms (one range only):

        bytes=START-END     bytes=START-     bytes=-SUFFIX
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[len("bytes=") :]
    if "," in spec:  # multi-range → let caller serve full body
        return None
    spec = spec.strip()
    if "-" not in spec:
        return None
    if total <= 0:
        raise RangeNotSatisfiableError("empty resource")

    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # suffix range: last N bytes
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else total - 1
    except ValueError:
        return None

    if start < 0 or start > end or start >= total:
        raise RangeNotSatisfiableError(f"bytes {start}-{end} outside 0-{total - 1}")
    end = min(end, total - 1)
    return ByteRange(start=start, end=end, total=total)


__all__ = ["ByteRange", "RangeNotSatisfiableError", "parse_range"]
