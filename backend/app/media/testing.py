"""In-memory fakes + tiny-asset builders for the media subsystem.

Lives in the app package (not ``tests/``) so both the unit tests and any future
local tooling can share one faithful in-memory object store. :class:`FakeMediaStore`
satisfies both :class:`app.media.store.MediaStoreBackend` and the broader
:class:`app.memory.interfaces.BlobStore` shape, so it can stand in for the real
``ObjectStore`` anywhere a blob backend is injected.

The asset builders produce **real, tiny, valid** files with ffmpeg/Pillow so the
ffmpeg-dependent tests exercise the genuine pipeline (and skip cleanly when no
ffmpeg binary is resolvable) rather than asserting against mocked bytes.
"""

from __future__ import annotations

from typing import Any


class FakeMediaStore:
    """A dict-backed object store for tests (no network, deterministic URLs)."""

    def __init__(self, *, public_base_url: str | None = None) -> None:
        self._objects: dict[str, tuple[bytes, str | None]] = {}
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        #: Observability counters for tests asserting dedup / call counts.
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0

    # -- MediaStoreBackend / BlobStore surface ------------------------------ #

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.put_calls += 1
        self._objects[key] = (bytes(data), content_type)

    def put_file(self, key: str, path: str, content_type: str | None = None) -> None:
        from pathlib import Path

        self.put_bytes(key, Path(path).read_bytes(), content_type)

    def get_bytes(self, key: str) -> bytes:
        self.get_calls += 1
        try:
            return self._objects[key][0]
        except KeyError as exc:  # mimic a NoSuchKey
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return key in self._objects

    def delete(self, key: str) -> None:
        self.delete_calls += 1
        self._objects.pop(key, None)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"https://signed.invalid/{key}?ttl={ttl}"

    def public_url(self, key: str) -> str | None:
        if self._public_base_url is None:
            return None
        return f"{self._public_base_url}/{key}"

    # -- test inspection ---------------------------------------------------- #

    def stored_keys(self) -> list[str]:
        """Sorted list of stored keys."""
        return sorted(self._objects)

    def content_type_of(self, key: str) -> str | None:
        """The content-type stored alongside ``key`` (or ``None``)."""
        return self._objects[key][1]

    def __len__(self) -> int:
        return len(self._objects)

    def __contains__(self, key: object) -> bool:
        return key in self._objects


# --------------------------------------------------------------------------- #
# Tiny real-asset builders (ffmpeg / Pillow) — skip cleanly when unavailable
# --------------------------------------------------------------------------- #


def tiny_png(
    width: int = 16, height: int = 16, color: tuple[int, int, int] = (40, 90, 160)
) -> bytes:
    """A minimal real PNG via Pillow (a solid colour block)."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def tiny_mp4(*, duration_s: float = 1.0, width: int = 64, height: int = 64, fps: int = 24) -> bytes:
    """A minimal real, playable mp4 (colour test source + silent audio).

    Uses the portably-resolved ffmpeg from :mod:`app.render.degrade`; raises
    :class:`app.render.degrade.FfmpegError` when no binary is available so the
    caller (a test) can skip.
    """
    import tempfile
    from pathlib import Path

    from app.render.degrade import get_ffmpeg_exe, run_ffmpeg

    ffmpeg = get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="kinora_media_test_") as tmp:
        out = Path(tmp) / "tiny.mp4"
        run_ffmpeg(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={width}x{height}:rate={fps}:duration={duration_s}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={duration_s}",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out),
            ]
        )
        data = out.read_bytes()
    return data


def media_available() -> bool:
    """True when both ffmpeg and Pillow are importable (for test skips)."""
    try:
        import PIL  # noqa: F401

        from app.render.degrade import ffmpeg_available

        return ffmpeg_available()
    except Exception:  # noqa: BLE001
        return False


def fake_metadata(**overrides: Any) -> Any:
    """Build an :class:`app.media.metadata.AssetMetadata` with sane defaults."""
    from app.media.metadata import AssetMetadata

    base: dict[str, Any] = {
        "storage_key": "media/by-hash/aa/bb/" + "a" * 64 + ".png",
        "content_type": "image/png",
        "content_hash": "a" * 64,
        "size_bytes": 123,
    }
    base.update(overrides)
    return AssetMetadata(**base)


__all__ = [
    "FakeMediaStore",
    "fake_metadata",
    "media_available",
    "tiny_mp4",
    "tiny_png",
]
