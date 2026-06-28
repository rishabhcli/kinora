"""The ``.kinora`` archive codec — a streaming, checksummed ZIP container.

This module is pure archive I/O: it knows nothing about books, canon, or the
database. It writes/reads three kinds of member:

* ``manifest.json``      — the :class:`ArchiveManifest`;
* ``data/<table>.jsonl`` — one JSON object per line (a logical "table" of rows);
* ``blobs/<sha256>``     — content-addressed object-store payloads;
  ``blobs/index.jsonl``  — the :class:`BlobRef` index for those payloads.

**Streaming & bounded memory.** Rows are appended a line at a time and blobs are
streamed through fixed-size chunks, so neither writer nor reader ever holds an
entire table or blob in memory beyond one row / one chunk (plus whatever the
stdlib ``zipfile`` buffers for a single member, which is unavoidable but bounded
by the largest single blob — itself chunk-fed). The *checksum* of each member is
computed incrementally as bytes flow, never by re-reading the whole member.

**Integrity.** Every data member and every blob payload is SHA-256'd while it is
written; the digests land in the manifest. :meth:`ArchiveReader.verify`
recomputes them on read and raises :class:`ChecksumMismatchError` on any drift, and
checks the manifest digest so a single value attests the archive.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable, Iterator
from typing import IO, Any

from app.dataportability.errors import ArchiveFormatError, ChecksumMismatchError
from app.dataportability.manifest import (
    ArchiveManifest,
    BlobRef,
)

#: Streaming chunk size for blob payloads (1 MiB) — the memory ceiling per blob.
BLOB_CHUNK = 1 << 20

_MANIFEST_NAME = "manifest.json"
_DATA_PREFIX = "data/"
_BLOB_PREFIX = "blobs/"
_BLOB_INDEX = "blobs/index.jsonl"
_DATA_SUFFIX = ".jsonl"


def _data_member(table: str) -> str:
    return f"{_DATA_PREFIX}{table}{_DATA_SUFFIX}"


def _blob_member(sha256: str) -> str:
    return f"{_BLOB_PREFIX}{sha256}"


class _HashingWriter:
    """A file-like wrapper that SHA-256s every byte written through it.

    Wraps the open zip-member stream so the digest is computed *as the member is
    written* (no second pass over the bytes).
    """

    def __init__(self, inner: IO[bytes]) -> None:
        self._inner = inner
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self._hash.update(data)
        self.size += len(data)
        return self._inner.write(data)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class ArchiveWriter:
    """Write a ``.kinora`` archive to a binary stream, member by member.

    Usage::

        with ArchiveWriter(stream, manifest) as w:
            for row in rows:
                w.write_row("shots", row)
            w.write_blob(sha, payload_iter, BlobRef(...))
        # manifest (with all checksums) is finalized on __exit__.

    The manifest is written **last** (after all data + blobs) because it carries
    their checksums; ``zipfile`` permits members in any order, and readers seek
    by name, so a trailing manifest is fine and keeps the writer single-pass.
    """

    def __init__(self, stream: IO[bytes], manifest: ArchiveManifest) -> None:
        self._zip = zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED)
        self._manifest = manifest
        self._counts: dict[str, int] = {}
        self._checksums: dict[str, str] = {}
        self._blob_refs: dict[str, BlobRef] = {}
        self._closed = False

    # -- rows ---------------------------------------------------------------- #

    def write_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        """Write an entire logical table from a row iterable; returns the count.

        The member is written in one streamed pass; ``rows`` is consumed lazily so
        a generator backed by a server-side DB cursor never materializes the table.
        """
        count = 0
        member = _data_member(table)
        with self._zip.open(member, mode="w") as raw:
            hw = _HashingWriter(raw)
            for row in rows:
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                hw.write(line.encode("utf-8"))
                hw.write(b"\n")
                count += 1
        self._checksums[member] = hw.hexdigest()
        self._counts[table] = count
        return count

    # -- blobs --------------------------------------------------------------- #

    def write_blob(
        self, sha256: str, chunks: Iterable[bytes], ref: BlobRef
    ) -> None:
        """Write one content-addressed blob payload from a chunk iterable.

        ``chunks`` streams the payload so a multi-hundred-MB clip never fully
        buffers. The blob is keyed by ``sha256`` (its content hash); a duplicate
        sha is written once and re-referenced (the caller is expected to dedup,
        but a second call with the same sha is a cheap no-op here).
        """
        if sha256 in self._blob_refs:
            return
        member = _blob_member(sha256)
        with self._zip.open(member, mode="w") as raw:
            hw = _HashingWriter(raw)
            for chunk in chunks:
                hw.write(chunk)
        actual = hw.hexdigest()
        if actual != sha256:
            raise ArchiveFormatError(
                f"blob content hash {actual} does not match declared sha {sha256}"
            )
        self._checksums[member] = actual
        self._blob_refs[sha256] = ref.model_copy(update={"sha256": sha256, "size": hw.size})

    def update_meta(self, extra: dict[str, Any]) -> None:
        """Merge extra entries into the manifest ``meta`` before finalize.

        Callers that compute summary metadata *while* writing (e.g. how many
        blobs were exported) use this so the figures land in the written
        manifest without reaching into writer internals.
        """
        self._manifest = self._manifest.model_copy(
            update={"meta": {**self._manifest.meta, **extra}}
        )

    # -- finalize ------------------------------------------------------------ #

    def _finalize(self) -> None:
        # Write the blob index (itself checksummed), then the manifest.
        index_lines = [
            self._blob_refs[sha].model_dump_json() for sha in sorted(self._blob_refs)
        ]
        index_bytes = ("\n".join(index_lines) + ("\n" if index_lines else "")).encode("utf-8")
        with self._zip.open(_BLOB_INDEX, mode="w") as raw:
            raw.write(index_bytes)
        self._checksums[_BLOB_INDEX] = hashlib.sha256(index_bytes).hexdigest()

        manifest = self._manifest.model_copy(
            update={"counts": dict(self._counts), "checksums": dict(self._checksums)}
        ).sealed()
        self._zip.writestr(_MANIFEST_NAME, manifest.to_json_bytes())

    def close(self) -> None:
        if self._closed:
            return
        self._finalize()
        self._zip.close()
        self._closed = True

    def __enter__(self) -> ArchiveWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        # Always close the zip; only finalize (write manifest) on a clean exit.
        if exc[0] is not None:
            if not self._closed:
                self._zip.close()
                self._closed = True
            return
        self.close()


class ArchiveReader:
    """Read a ``.kinora`` archive from a binary stream (random-access by name).

    The reader verifies checksums lazily — :meth:`verify` walks the whole archive
    once; :meth:`read_rows` / :meth:`open_blob` can also verify the single member
    they touch so a partial read still fails closed on tampering.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        try:
            self._zip = zipfile.ZipFile(stream, mode="r")
        except zipfile.BadZipFile as exc:
            raise ArchiveFormatError("not a valid zip archive") from exc
        self._manifest = self._read_manifest()

    def _read_manifest(self) -> ArchiveManifest:
        try:
            raw = self._zip.read(_MANIFEST_NAME)
        except KeyError as exc:
            raise ArchiveFormatError("archive is missing manifest.json") from exc
        try:
            return ArchiveManifest.from_json_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - any parse failure => malformed
            raise ArchiveFormatError(f"manifest.json is unreadable: {exc}") from exc

    @property
    def manifest(self) -> ArchiveManifest:
        """The parsed (un-migrated) manifest."""
        return self._manifest

    def tables(self) -> list[str]:
        """The logical table names present (``data/<table>.jsonl`` members)."""
        out: list[str] = []
        for name in self._zip.namelist():
            if name.startswith(_DATA_PREFIX) and name.endswith(_DATA_SUFFIX):
                out.append(name[len(_DATA_PREFIX) : -len(_DATA_SUFFIX)])
        return sorted(out)

    def read_rows(self, table: str, *, verify: bool = True) -> Iterator[dict[str, Any]]:
        """Yield every row of a logical table, one parsed dict at a time.

        With ``verify`` (default) the member's checksum is recomputed while it is
        streamed and checked at end-of-member, so a corrupted row table is caught
        even on a streamed read. Missing tables yield nothing (an export with no
        rows of that kind simply omits the member).
        """
        member = _data_member(table)
        if member not in self._zip.namelist():
            return
        yield from self._read_jsonl(member, verify=verify)

    def _read_jsonl(self, member: str, *, verify: bool) -> Iterator[dict[str, Any]]:
        expected = self._manifest.checksums.get(member)
        hasher = hashlib.sha256()
        buf = b""
        with self._zip.open(member, mode="r") as raw:
            while True:
                chunk = raw.read(BLOB_CHUNK)
                if not chunk:
                    break
                if verify:
                    hasher.update(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if line:
                        yield json.loads(line)
            if buf.strip():
                yield json.loads(buf)
        if verify and expected is not None and hasher.hexdigest() != expected:
            raise ChecksumMismatchError(member, expected, hasher.hexdigest())

    def blob_refs(self) -> dict[str, BlobRef]:
        """The blob index (``sha256 -> BlobRef``); empty if there are no blobs."""
        if _BLOB_INDEX not in self._zip.namelist():
            return {}
        refs: dict[str, BlobRef] = {}
        raw = self._zip.read(_BLOB_INDEX)
        for line in raw.splitlines():
            if line.strip():
                ref = BlobRef.model_validate_json(line)
                refs[ref.sha256] = ref
        return refs

    def open_blob(self, sha256: str, *, verify: bool = True) -> Iterator[bytes]:
        """Stream one blob payload in chunks, verifying its content hash.

        Because blobs are content-addressed (the member name *is* the sha), the
        verification recomputes the hash and compares it to the member name, which
        catches any tampering without needing the manifest at all.
        """
        member = _blob_member(sha256)
        if member not in self._zip.namelist():
            raise ArchiveFormatError(f"archive is missing blob {sha256}")
        hasher = hashlib.sha256()
        with self._zip.open(member, mode="r") as raw:
            while True:
                chunk = raw.read(BLOB_CHUNK)
                if not chunk:
                    break
                if verify:
                    hasher.update(chunk)
                yield chunk
        if verify and hasher.hexdigest() != sha256:
            raise ChecksumMismatchError(member, sha256, hasher.hexdigest())

    def read_blob(self, sha256: str, *, verify: bool = True) -> bytes:
        """Read one whole blob into memory (convenience for small payloads)."""
        return b"".join(self.open_blob(sha256, verify=verify))

    def verify(self) -> None:
        """Verify the whole archive: manifest digest + every member checksum.

        Raises :class:`ChecksumMismatchError` on the first drift (the manifest digest
        first, then each data member, then the blob index, then every blob). A
        clean return means the archive is byte-intact.
        """
        if self._manifest.manifest_digest:
            recomputed = self._manifest.compute_digest()
            if recomputed != self._manifest.manifest_digest:
                raise ChecksumMismatchError(
                    _MANIFEST_NAME, self._manifest.manifest_digest, recomputed
                )
        names = set(self._zip.namelist())
        for member, expected in sorted(self._manifest.checksums.items()):
            if member not in names:
                raise ArchiveFormatError(f"manifest references missing member {member!r}")
            actual = self._member_digest(member)
            if actual != expected:
                raise ChecksumMismatchError(member, expected, actual)
        # Content-address check: every blob payload hashes to its own name.
        for name in names:
            if name.startswith(_BLOB_PREFIX) and name != _BLOB_INDEX:
                sha = name[len(_BLOB_PREFIX) :]
                actual = self._member_digest(name)
                if actual != sha:
                    raise ChecksumMismatchError(name, sha, actual)

    def _member_digest(self, member: str) -> str:
        hasher = hashlib.sha256()
        with self._zip.open(member, mode="r") as raw:
            while True:
                chunk = raw.read(BLOB_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_writer_to_bytes(manifest: ArchiveManifest) -> tuple[ArchiveWriter, io.BytesIO]:
    """Build an :class:`ArchiveWriter` over an in-memory buffer (tests/streaming)."""
    buffer = io.BytesIO()
    return ArchiveWriter(buffer, manifest), buffer


__all__ = [
    "BLOB_CHUNK",
    "ArchiveReader",
    "ArchiveWriter",
    "open_writer_to_bytes",
]
