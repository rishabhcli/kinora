"""Backup-set manifest verification + chain assembly.

Two concerns live here, both pure given a :class:`~app.dr.interfaces.BackupRepository`:

* **Single-manifest integrity** (:func:`verify_manifest`): recompute every
  segment's checksum from its payload and the roll-up ``content_hash`` over the
  segments; any mismatch is an :class:`~app.dr.errors.IntegrityError` naming the
  bad segment. This is the gate every restore runs *before* touching state — a
  bit-rotted or tampered backup is rejected, never applied.

* **Chain assembly** (:func:`resolve_chain`): walk an incremental back through
  its ``parent_id`` links to the founding full backup, returning the ordered
  ``[full, inc1, inc2, …, head]`` list a restore replays in sequence. The walk
  enforces the chain invariants — it terminates at exactly one full, every link
  resolves, and the pinned positions are strictly increasing — so a forged or
  broken chain is a :class:`~app.dr.errors.ChainError`, not a silent partial
  restore.
"""

from __future__ import annotations

from app.dr.checksums import Checksum, combine
from app.dr.errors import ChainError, IntegrityError, ManifestError
from app.dr.interfaces import BackupRepository
from app.dr.models import BackupManifest, BackupTier, SegmentKind

#: The five segment kinds a well-formed snapshot must carry.
_REQUIRED_SEGMENTS = frozenset(SegmentKind)

#: A defensive cap so a cyclic ``parent_id`` (corruption/forgery) cannot loop
#: forever; far above any realistic chain length.
_MAX_CHAIN = 100_000


def verify_manifest(manifest: BackupManifest) -> None:
    """Verify a manifest is structurally sound and every checksum matches.

    Raises:
        ManifestError: a structural fault (missing/duplicate segment kind,
            unknown format version).
        IntegrityError: a segment payload does not match its recorded checksum,
            or the roll-up ``content_hash`` does not match the segments.
    """
    if manifest.format_version != 1:
        raise ManifestError(f"unsupported backup format_version {manifest.format_version}")

    kinds = [s.kind for s in manifest.segments]
    if len(kinds) != len(set(kinds)):
        raise ManifestError("duplicate segment kind in manifest")
    missing = _REQUIRED_SEGMENTS - set(kinds)
    if missing:
        names = ", ".join(sorted(str(k) for k in missing))
        raise ManifestError(f"manifest missing required segment(s): {names}")

    # Per-segment checksum recompute.
    for seg in manifest.segments:
        recomputed = Checksum.of(seg.payload)
        if recomputed.value != seg.checksum.value or recomputed.algorithm != seg.checksum.algorithm:
            raise IntegrityError(
                segment=str(seg.kind),
                expected=seg.checksum.value,
                actual=recomputed.value,
            )

    # Roll-up content hash over the (recorded) segment checksums.
    rollup = combine(
        *(
            Checksum(algorithm=s.checksum.algorithm, value=s.checksum.value)
            for s in manifest.segments
        )
    )
    declared = manifest.descriptor.content_hash
    if rollup.value != declared.value or rollup.algorithm != declared.algorithm:
        raise IntegrityError(
            segment="content_hash",
            expected=declared.value,
            actual=rollup.value,
        )


async def resolve_chain(
    repo: BackupRepository,
    head_id: str,
    *,
    verify: bool = True,
) -> list[BackupManifest]:
    """Return the ordered restore chain ``[full, …, head]`` for ``head_id``.

    Walks ``parent_id`` from the head back to the founding full, then reverses to
    replay order. Enforces:

    * every ``parent_id`` resolves in ``repo`` (else :class:`ChainError`);
    * the walk terminates at exactly one FULL (a chain that bottoms out on an
      incremental with no parent is broken);
    * pinned positions are strictly increasing from full to head, and each link's
      ``base_position`` equals its parent's ``pinned_position`` (a contiguous,
      gap-free, non-overlapping chain).

    When ``verify`` is set, each manifest in the chain is checksum-verified too,
    so a single call validates both lineage and integrity.

    Raises:
        ChainError: any lineage invariant is violated.
        IntegrityError: ``verify`` and a segment checksum mismatches.
    """
    chain: list[BackupManifest] = []
    cursor: str | None = head_id
    seen: set[str] = set()

    while cursor is not None:
        if cursor in seen:
            raise ChainError(f"cycle detected in backup chain at {cursor!r}")
        if len(chain) > _MAX_CHAIN:
            raise ChainError("backup chain exceeds the maximum supported length")
        seen.add(cursor)

        manifest = await repo.get(cursor)
        if manifest is None:
            raise ChainError(f"backup {cursor!r} referenced by the chain is missing")
        if verify:
            verify_manifest(manifest)
        chain.append(manifest)

        desc = manifest.descriptor
        if desc.tier is BackupTier.FULL:
            cursor = None
        else:
            if desc.parent_id is None:
                raise ChainError(
                    f"incremental {cursor!r} has no parent_id — chain does not reach a full"
                )
            cursor = desc.parent_id

    # ``chain`` is head→full; reverse to full→head (replay order).
    chain.reverse()

    if chain[0].descriptor.tier is not BackupTier.FULL:
        raise ChainError("restore chain does not begin with a full backup")

    # Contiguity + monotonicity across the chain.
    prev = chain[0]
    if prev.descriptor.base_position != 0:
        raise ChainError("the founding full backup must have base_position == 0")
    for nxt in chain[1:]:
        if nxt.descriptor.base_position != prev.descriptor.pinned_position:
            raise ChainError(
                f"chain gap/overlap: {nxt.descriptor.snapshot_id!r} starts at "
                f"{nxt.descriptor.base_position} but parent ends at "
                f"{prev.descriptor.pinned_position}"
            )
        if nxt.descriptor.pinned_position < prev.descriptor.pinned_position:
            raise ChainError("chain pinned positions are not non-decreasing")
        prev = nxt

    return chain


__all__ = ["resolve_chain", "verify_manifest"]
