"""Content-addressed media store with multipart / resumable upload.

:class:`MediaStore` wraps a low-level blob backend (the real
:class:`app.storage.object_store.ObjectStore`, or any object satisfying the
:class:`MediaStoreBackend` Protocol) and adds the value the raw client lacks:

* **Content-addressed put** (``put_content_addressed``): hash the bytes, derive
  the §8.7-style ``media/by-hash/...`` key, and **skip the upload entirely** when
  that key already exists. Two identical Ken-Burns cards / posters cost one PUT
  and one stored object, forever.
* **Checksum-verified get** (``get_verified``): download and re-hash, raising
  :class:`ChecksumMismatchError` if the bytes drifted from the expected digest.
* **Multipart / resumable upload** (:class:`MultipartUpload`): accumulate parts
  out of one PUT's size limit, with a streaming sha256 so the final object is
  also content-addressable; an interrupted upload can resume from the parts
  already accepted.
* **Browser-correct URLs** (``url_for``) via :mod:`app.media.urls`.

Everything is synchronous + blocking (boto3 is blocking); callers that need
async run it through ``anyio.to_thread`` exactly as the render pipeline already
does for the raw store.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from app.media.errors import MultipartError, UploadNotFoundError
from app.media.hashing import content_address_key, sha256_hex
from app.media.metadata import AssetMetadata, MediaAssetKind, guess_content_type
from app.media.urls import DEFAULT_TTL_S, media_url

#: Minimum non-final multipart part size. Mirrors S3's 5 MiB floor so a part
#: stream that is valid here is valid against real S3/OSS too.
MIN_PART_BYTES = 5 * 1024 * 1024


@runtime_checkable
class MediaStoreBackend(Protocol):
    """The blob operations :class:`MediaStore` needs.

    :class:`app.storage.object_store.ObjectStore` satisfies this verbatim, so
    the store layers on the existing client with zero changes to it; tests
    inject :class:`app.media.testing.FakeMediaStore`.
    """

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str: ...

    def public_url(self, key: str) -> str | None: ...


class MultipartUpload:
    """A resumable, content-addressed multipart upload buffer.

    Parts are appended in order; each is hashed into a running sha256 so the
    completed object can be stored at its content-address key. Non-final parts
    must be at least :data:`MIN_PART_BYTES` (S3 rule) — the final/only part may
    be smaller. The buffer is purely in-memory here (the bundled MinIO/dev path
    and tests have no true server-side multipart need); the abstraction is what
    matters so a real ``create_multipart_upload`` backend can slot in later.
    """

    def __init__(self, upload_id: str, *, content_type: str | None = None) -> None:
        self.upload_id = upload_id
        self.content_type = content_type
        self._parts: list[bytes] = []
        self._hash = hashlib.sha256()
        self._completed = False
        self._aborted = False

    @property
    def part_count(self) -> int:
        """Number of accepted parts so far."""
        return len(self._parts)

    @property
    def size_bytes(self) -> int:
        """Total accumulated byte length."""
        return sum(len(p) for p in self._parts)

    @property
    def is_open(self) -> bool:
        """True until the upload is completed or aborted."""
        return not (self._completed or self._aborted)

    def upload_part(self, data: bytes, *, is_final: bool = False) -> int:
        """Append a part; returns its 1-based part number.

        Non-final parts below :data:`MIN_PART_BYTES` are rejected (the S3 rule),
        which keeps the dev path honest about what a real backend would accept.
        """
        if not self.is_open:
            raise UploadNotFoundError(self.upload_id)
        if not is_final and len(data) < MIN_PART_BYTES:
            raise MultipartError(
                f"non-final part {self.part_count + 1} is {len(data)}B "
                f"(< {MIN_PART_BYTES}B minimum)"
            )
        self._parts.append(data)
        self._hash.update(data)
        return len(self._parts)

    def assembled(self) -> bytes:
        """Concatenate accepted parts (for resume inspection / completion)."""
        return b"".join(self._parts)

    def running_digest(self) -> str:
        """The sha256 of all parts accepted so far (stable, incremental)."""
        return self._hash.hexdigest()

    def _mark_completed(self) -> None:
        self._completed = True

    def _mark_aborted(self) -> None:
        self._aborted = True


class MediaStore:
    """Content-addressed, multipart-capable façade over a blob backend."""

    def __init__(self, backend: MediaStoreBackend, *, url_ttl: int = DEFAULT_TTL_S) -> None:
        self._backend = backend
        self._url_ttl = url_ttl
        self._uploads: dict[str, MultipartUpload] = {}

    # -- direct passthroughs ------------------------------------------------- #

    @property
    def backend(self) -> MediaStoreBackend:
        """The underlying blob backend (for callers that need raw access)."""
        return self._backend

    def put(self, key: str, data: bytes, content_type: str | None = None) -> AssetMetadata:
        """Store ``data`` at an explicit ``key`` and return its metadata."""
        ctype = content_type or guess_content_type(key)
        self._backend.put_bytes(key, data, ctype)
        return AssetMetadata(
            storage_key=key,
            content_type=ctype,
            content_hash=sha256_hex(data),
            size_bytes=len(data),
        )

    def get(self, key: str) -> bytes:
        """Download the bytes at ``key``."""
        return self._backend.get_bytes(key)

    def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""
        return self._backend.exists(key)

    def delete(self, key: str) -> None:
        """Delete ``key`` (idempotent at the backend)."""
        self._backend.delete(key)

    def url_for(self, key: str, *, ttl: int | None = None) -> str:
        """A browser-reachable URL for ``key`` (public base else signed)."""
        return media_url(self._backend, key, ttl=ttl if ttl is not None else self._url_ttl)

    # -- content addressing -------------------------------------------------- #

    def address_of(self, data: bytes, *, suffix: str = "", prefix: str | None = None) -> str:
        """The deterministic content-address key for ``data`` (no upload)."""
        return content_address_key(sha256_hex(data), suffix=suffix, prefix=prefix)

    def put_content_addressed(
        self,
        data: bytes,
        *,
        suffix: str = "",
        content_type: str | None = None,
        kind: MediaAssetKind = MediaAssetKind.OTHER,
        prefix: str | None = None,
        book_id: str | None = None,
    ) -> tuple[AssetMetadata, bool]:
        """Store ``data`` at its content-address key, deduplicating by bytes.

        Returns ``(metadata, deduplicated)`` where ``deduplicated`` is ``True``
        when the bytes were already present and **no upload happened**. The
        returned key/digest are identical either way, so callers can record a new
        reference to the existing blob without caring whether they paid for it.
        """
        digest = sha256_hex(data)
        key = content_address_key(digest, suffix=suffix, prefix=prefix)
        ctype = content_type or guess_content_type(key)
        meta = AssetMetadata(
            storage_key=key,
            kind=kind,
            content_type=ctype,
            content_hash=digest,
            size_bytes=len(data),
            book_id=book_id,
        )
        if self._backend.exists(key):
            return meta, True
        self._backend.put_bytes(key, data, ctype)
        return meta, False

    def get_verified(self, key: str, expected_hash: str) -> bytes:
        """Download ``key`` and assert its sha256 equals ``expected_hash``."""
        data = self._backend.get_bytes(key)
        actual = sha256_hex(data)
        if actual != expected_hash.lower():
            from app.media.errors import ChecksumMismatchError

            raise ChecksumMismatchError(key, expected_hash.lower(), actual)
        return data

    # -- multipart / resumable ---------------------------------------------- #

    def create_multipart(
        self, *, content_type: str | None = None, upload_id: str | None = None
    ) -> MultipartUpload:
        """Begin a resumable multipart upload; returns its handle."""
        import uuid

        uid = upload_id or uuid.uuid4().hex
        upload = MultipartUpload(uid, content_type=content_type)
        self._uploads[uid] = upload
        return upload

    def get_multipart(self, upload_id: str) -> MultipartUpload:
        """Look up an in-progress multipart upload (for resume)."""
        upload = self._uploads.get(upload_id)
        if upload is None or not upload.is_open:
            raise UploadNotFoundError(upload_id)
        return upload

    def complete_multipart(
        self,
        upload: MultipartUpload | str,
        *,
        suffix: str = "",
        kind: MediaAssetKind = MediaAssetKind.OTHER,
        prefix: str | None = None,
        book_id: str | None = None,
        expected_hash: str | None = None,
    ) -> tuple[AssetMetadata, bool]:
        """Finalise a multipart upload as one content-addressed object.

        The assembled bytes are stored at their content-address key (so a
        multipart upload also dedups). ``expected_hash`` (if given) is checked
        against the assembled digest. Returns ``(metadata, deduplicated)``.
        """
        handle = upload if isinstance(upload, MultipartUpload) else self.get_multipart(upload)
        if not handle.is_open:
            raise UploadNotFoundError(handle.upload_id)
        data = handle.assembled()
        digest = handle.running_digest()
        if expected_hash is not None and digest != expected_hash.lower():
            from app.media.errors import ChecksumMismatchError

            raise ChecksumMismatchError(handle.upload_id, expected_hash.lower(), digest)
        meta, dedup = self.put_content_addressed(
            data,
            suffix=suffix,
            content_type=handle.content_type,
            kind=kind,
            prefix=prefix,
            book_id=book_id,
        )
        handle._mark_completed()
        self._uploads.pop(handle.upload_id, None)
        return meta, dedup

    def abort_multipart(self, upload: MultipartUpload | str) -> None:
        """Discard an in-progress multipart upload (frees the buffer)."""
        uid = upload.upload_id if isinstance(upload, MultipartUpload) else upload
        handle = self._uploads.pop(uid, None)
        if handle is not None:
            handle._mark_aborted()

    def put_stream(
        self,
        chunks: Iterable[bytes],
        *,
        suffix: str = "",
        content_type: str | None = None,
        kind: MediaAssetKind = MediaAssetKind.OTHER,
        part_bytes: int = MIN_PART_BYTES,
        prefix: str | None = None,
        book_id: str | None = None,
    ) -> tuple[AssetMetadata, bool]:
        """Upload an iterable of byte chunks via multipart, content-addressed.

        Chunks are coalesced into ``part_bytes``-sized parts; the trailing
        remainder becomes the final (possibly small) part. Convenience over
        :meth:`create_multipart` for the common "I have a generator of bytes"
        case (e.g. streaming a large source PDF or a downloaded provider video).
        """
        upload = self.create_multipart(content_type=content_type)
        buf = bytearray()

        def _flush(final: bool) -> None:
            if buf:
                upload.upload_part(bytes(buf), is_final=final)
                buf.clear()

        for chunk in chunks:
            buf.extend(chunk)
            while len(buf) >= part_bytes:
                upload.upload_part(bytes(buf[:part_bytes]), is_final=False)
                del buf[:part_bytes]
        _flush(final=True)
        return self.complete_multipart(
            upload, suffix=suffix, kind=kind, prefix=prefix, book_id=book_id
        )


def chunked(data: bytes, size: int) -> Iterator[bytes]:
    """Yield ``size``-byte chunks of ``data`` (test/util helper)."""
    view = io.BytesIO(data)
    while True:
        block = view.read(size)
        if not block:
            return
        yield block


__all__ = [
    "MIN_PART_BYTES",
    "MediaStore",
    "MediaStoreBackend",
    "MultipartUpload",
    "chunked",
]
