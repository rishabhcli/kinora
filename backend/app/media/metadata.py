"""Per-asset metadata + MIME/extension helpers (pure).

:class:`AssetMetadata` is the durable, transport-agnostic description of one
managed blob: its key, content digest, MIME type, size, and (for AV/visual
assets) geometry/duration. It is what the repository persists and what the API
projects. Kept a frozen pydantic model so it round-trips to/from JSONB and over
HTTP without bespoke serialisers.

The MIME/extension tables are intentionally small and explicit — only the
formats Kinora actually produces (mp4/png/jpg/webp/wav/m4a/pdf/epub/vtt/HLS/DASH)
— rather than leaning on the stdlib ``mimetypes`` registry, which differs across
platforms and would make tests non-deterministic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.media.kinds import MediaAssetKind

#: extension → MIME for the formats this subsystem emits/manages.
_EXT_TO_MIME: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".vtt": "text/vtt",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mpd": "application/dash+xml",
    ".json": "application/json",
    ".txt": "text/plain",
}

#: MIME → canonical extension (first/preferred spelling wins).
_MIME_TO_EXT: dict[str, str] = {}
for _ext, _mime in _EXT_TO_MIME.items():
    _MIME_TO_EXT.setdefault(_mime, _ext)

#: The neutral fallback when a format is unknown.
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def guess_content_type(key_or_name: str, *, default: str = DEFAULT_CONTENT_TYPE) -> str:
    """Infer a MIME type from a key/filename's extension."""
    lower = key_or_name.lower()
    dot = lower.rfind(".")
    if dot == -1:
        return default
    return _EXT_TO_MIME.get(lower[dot:], default)


def suffix_for(content_type: str, *, default: str = "") -> str:
    """The canonical file extension (with leading dot) for a MIME type."""
    return _MIME_TO_EXT.get(content_type.split(";", 1)[0].strip().lower(), default)


def sniff_image_suffix(raw: bytes) -> str:
    """Sniff an image's extension from its magic bytes (png/jpg/webp/gif)."""
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".png"


class AssetMetadata(BaseModel):
    """Durable description of one managed media blob."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    storage_key: str
    kind: MediaAssetKind = MediaAssetKind.OTHER
    content_type: str = DEFAULT_CONTENT_TYPE
    content_hash: str | None = None
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    etag: str | None = None
    book_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_visual(self) -> bool:
        """True when the asset carries pixels (has a width/height)."""
        return self.width is not None and self.height is not None

    @property
    def is_timed(self) -> bool:
        """True when the asset has a duration (audio/video)."""
        return self.duration_s is not None

    @property
    def aspect_ratio(self) -> float | None:
        """``width / height`` when both are known, else ``None``."""
        if self.width and self.height:
            return self.width / self.height
        return None

    def with_meta(self, **extra: Any) -> AssetMetadata:
        """Return a copy with additional ``meta`` keys merged in."""
        merged = {**self.meta, **extra}
        return self.model_copy(update={"meta": merged})


__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "AssetMetadata",
    "guess_content_type",
    "sniff_image_suffix",
    "suffix_for",
]
