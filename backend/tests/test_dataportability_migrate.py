"""Unit tests for the archive-format migration chain (no infrastructure).

Exercises the migration registry + :func:`migrate_reader` by temporarily
registering a synthetic ``v1 -> v2`` transform and bumping the current version,
so the chain machinery is covered before the first real schema change needs it.
"""

from __future__ import annotations

import io

import pytest

from app.dataportability import migrate as migrate_mod
from app.dataportability.codec import ArchiveReader, open_writer_to_bytes
from app.dataportability.errors import PortabilityError, UnsupportedArchiveVersionError
from app.dataportability.manifest import ArchiveKind, ArchiveManifest
from app.dataportability.migrate import migrate_reader, registered_versions


def _archive(format_version: int, rows: dict[str, list[dict]]) -> bytes:
    manifest = ArchiveManifest(format_version=format_version, kind=ArchiveKind.BOOK)
    writer, buf = open_writer_to_bytes(manifest)
    with writer as w:
        for table, table_rows in rows.items():
            w.write_rows(table, table_rows)
    return buf.getvalue()


def test_current_version_is_passthrough() -> None:
    data = _archive(migrate_mod.CURRENT_FORMAT_VERSION, {"shots": [{"id": "s1"}]})
    with ArchiveReader(io.BytesIO(data)) as reader:
        view = migrate_reader(reader)
        assert view.manifest.format_version == migrate_mod.CURRENT_FORMAT_VERSION
        assert list(view.read_rows("shots")) == [{"id": "s1"}]


def test_future_version_rejected() -> None:
    data = _archive(migrate_mod.CURRENT_FORMAT_VERSION + 5, {})
    with ArchiveReader(io.BytesIO(data)) as reader, pytest.raises(UnsupportedArchiveVersionError):
        migrate_reader(reader)


def test_registered_versions_initially_empty() -> None:
    # v1 is current with no real transforms registered yet.
    assert registered_versions() == []


def test_synthetic_migration_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend the current version is 2 and register a v1 -> v2 transform that
    # renames a column in every shot row. Build a v1 archive, migrate it, and
    # assert the transform ran.
    fresh_registry: dict[int, migrate_mod.MigrationFn] = {}
    monkeypatch.setattr(migrate_mod, "_REGISTRY", fresh_registry)
    monkeypatch.setattr(migrate_mod, "CURRENT_FORMAT_VERSION", 2)

    @migrate_mod.migration(from_version=1)
    def _v1_to_v2(manifest: ArchiveManifest, rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
        for row in rows.get("shots", []):
            if "old_name" in row:
                row["new_name"] = row.pop("old_name")
        return rows

    data = _archive(1, {"shots": [{"id": "s1", "old_name": "hi"}]})
    with ArchiveReader(io.BytesIO(data)) as reader:
        view = migrate_reader(reader)
        assert view.manifest.format_version == 2
        migrated = list(view.read_rows("shots"))
        assert migrated == [{"id": "s1", "new_name": "hi"}]
    assert migrate_mod.registered_versions() == [1]


def test_chain_gap_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    # current=3 with only a v1->v2 transform leaves a gap at v2 -> loud failure.
    fresh_registry: dict[int, migrate_mod.MigrationFn] = {}
    monkeypatch.setattr(migrate_mod, "_REGISTRY", fresh_registry)
    monkeypatch.setattr(migrate_mod, "CURRENT_FORMAT_VERSION", 3)

    @migrate_mod.migration(from_version=1)
    def _v1_to_v2(manifest: ArchiveManifest, rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
        return rows

    data = _archive(1, {})
    with ArchiveReader(io.BytesIO(data)) as reader, pytest.raises(PortabilityError):
        migrate_reader(reader)


def test_duplicate_migration_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_registry: dict[int, migrate_mod.MigrationFn] = {}
    monkeypatch.setattr(migrate_mod, "_REGISTRY", fresh_registry)

    @migrate_mod.migration(from_version=1)
    def _a(manifest: ArchiveManifest, rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
        return rows

    with pytest.raises(PortabilityError):

        @migrate_mod.migration(from_version=1)
        def _b(manifest: ArchiveManifest, rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
            return rows
