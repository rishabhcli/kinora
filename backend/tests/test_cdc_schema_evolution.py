"""Unit tests for schema registry + evolution reconciliation (no infra)."""

from __future__ import annotations

from app.streaming.cdc.schema import (
    Column,
    SchemaRegistry,
    TableSchema,
    migrate_row,
    reconcile,
)


def _schema(version: int, cols: dict[str, str]) -> TableSchema:
    return TableSchema.from_mapping("books", version, cols)


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = _schema(1, {"id": "str", "title": "str"})
    b = _schema(1, {"title": "str", "id": "str"})  # order should not matter
    c = _schema(1, {"id": "str", "title": "str", "author": "str"})
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_reconcile_add_drop_retype() -> None:
    old = _schema(1, {"id": "str", "title": "str", "legacy": "int"})
    new = _schema(2, {"id": "str", "title": "str", "author": "str", "num_pages": "int"})
    # legacy was an int; pretend title widened type in new.
    new = TableSchema(
        table="books",
        version=2,
        columns=(
            Column("id", "str"),
            Column("title", "text"),  # retyped str -> text
            Column("author", "str"),
            Column("num_pages", "int"),
        ),
    )
    delta = reconcile(old, new)
    assert {c.name for c in delta.added} == {"author", "num_pages"}
    assert {c.name for c in delta.dropped} == {"legacy"}
    assert [(o.name, n.type) for o, n in delta.retyped] == [("title", "text")]
    assert not delta.breaking


def test_reconcile_rename_carries_value() -> None:
    old = _schema(1, {"id": "str", "old_name": "str"})
    new = _schema(2, {"id": "str", "new_name": "str"})
    delta = reconcile(old, new, renames={"old_name": "new_name"})
    assert delta.added == () and delta.dropped == ()  # rename, not add+drop
    migrated = migrate_row({"id": "b1", "old_name": "Dune"}, new, delta)
    assert migrated == {"id": "b1", "new_name": "Dune"}


def test_pk_change_is_breaking() -> None:
    old = TableSchema("t", 1, (Column("id"),), primary_key=("id",))
    new = TableSchema("t", 2, (Column("id"), Column("k")), primary_key=("id", "k"))
    delta = reconcile(old, new)
    assert delta.pk_changed and delta.breaking


def test_migrate_row_backfills_added_defaults() -> None:
    new = TableSchema(
        "books",
        2,
        (Column("id", "str"), Column("status", "str", default="importing")),
    )
    delta = reconcile(_schema(1, {"id": "str"}), new)
    migrated = migrate_row({"id": "b1"}, new, delta)
    assert migrated == {"id": "b1", "status": "importing"}


def test_registry_multistep_migration() -> None:
    reg = SchemaRegistry()
    reg.register(_schema(1, {"id": "str", "title": "str"}))
    reg.register(_schema(2, {"id": "str", "title": "str", "author": "str"}))
    reg.register(
        TableSchema(
            "books",
            3,
            (
                Column("id", "str"),
                Column("title", "str"),
                Column("author", "str"),
                Column("cover_key", "str", default="none"),
            ),
        )
    )
    # A row written at v1 is brought all the way to v3.
    out = reg.migrate({"id": "b1", "title": "Dune"}, "books", from_version=1)
    assert out == {"id": "b1", "title": "Dune", "author": None, "cover_key": "none"}
    latest = reg.latest("books")
    assert latest is not None and latest.version == 3


def test_registry_no_op_when_already_latest() -> None:
    reg = SchemaRegistry()
    reg.register(_schema(1, {"id": "str"}))
    assert reg.migrate({"id": "b1"}, "books", from_version=1) == {"id": "b1"}
    # Unknown table is a no-op too.
    assert reg.migrate({"id": "x"}, "unknown", from_version=1) == {"id": "x"}
