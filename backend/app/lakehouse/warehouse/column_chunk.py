"""An encoded column chunk — the smallest independently-readable storage unit.

A :class:`ColumnChunk` pairs an encoded blob (from
:mod:`app.lakehouse.warehouse.encoding`) with its
:class:`~app.lakehouse.warehouse.statistics.ColumnStatistics` and an optional
:class:`~app.lakehouse.warehouse.statistics.ZoneMap`. It knows its own dtype so it
can decode without an external schema, and it is byte-serialisable so it can be
embedded into a :class:`~app.lakehouse.warehouse.columnar.ColumnarFile`.

Writing a chunk chooses the best codec automatically; reading reconstructs the
exact original vector. Statistics are computed *before* encoding (from the source
vector) so they describe logical values, not bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.lakehouse.warehouse.encoding import (
    Encoding,
    decode,
    encode,
    encode_auto,
    read_uvarint,
    write_uvarint,
)
from app.lakehouse.warehouse.statistics import (
    ColumnStatistics,
    ZoneMap,
    build_zonemap,
    compute_statistics,
)
from app.lakehouse.warehouse.types import ColumnVector, LogicalType


@dataclass(frozen=True, slots=True)
class ColumnChunk:
    """An encoded column plus its statistics and (optional) zonemap."""

    name: str
    dtype: LogicalType
    encoding: Encoding
    num_rows: int
    statistics: ColumnStatistics
    data: bytes
    zonemap: ZoneMap | None = None

    @classmethod
    def write(
        cls,
        name: str,
        vec: ColumnVector,
        *,
        encoding: Encoding | None = None,
        zone_size: int | None = None,
    ) -> ColumnChunk:
        """Encode ``vec`` into a chunk.

        ``encoding=None`` auto-selects the smallest codec. ``zone_size`` (when set)
        attaches a zonemap for sub-chunk skipping.
        """
        stats = compute_statistics(vec)
        if encoding is None:
            enc, blob = encode_auto(vec)
        else:
            enc, blob = encoding, encode(vec, encoding)
        zmap = build_zonemap(vec, zone_size) if zone_size else None
        return cls(
            name=name,
            dtype=vec.dtype,
            encoding=enc,
            num_rows=len(vec),
            statistics=stats,
            data=blob,
            zonemap=zmap,
        )

    def read(self) -> ColumnVector:
        """Decode the chunk back into the exact source vector."""
        return decode(self.data, self.dtype)

    def serialize(self) -> bytes:
        """A self-describing byte frame (name + dtype + payload) for the file format."""
        out = bytearray()
        name_bytes = self.name.encode("utf-8")
        write_uvarint(out, len(name_bytes))
        out.extend(name_bytes)
        dtype_bytes = self.dtype.value.encode("utf-8")
        write_uvarint(out, len(dtype_bytes))
        out.extend(dtype_bytes)
        write_uvarint(out, len(self.data))
        out.extend(self.data)
        return bytes(out)

    @classmethod
    def deserialize(cls, buf: bytes, *, zone_size: int | None = None) -> ColumnChunk:
        """Reconstruct a chunk from :meth:`serialize` output."""
        pos = 0
        nlen, pos = read_uvarint(buf, pos)
        name = buf[pos : pos + nlen].decode("utf-8")
        pos += nlen
        dlen, pos = read_uvarint(buf, pos)
        dtype = LogicalType(buf[pos : pos + dlen].decode("utf-8"))
        pos += dlen
        blen, pos = read_uvarint(buf, pos)
        data = bytes(buf[pos : pos + blen])
        vec = decode(data, dtype)
        return cls.write(name, vec, zone_size=zone_size)


__all__ = ["ColumnChunk"]
