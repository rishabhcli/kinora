"""Unit tests for the composable report model + JSON round-trip."""

from __future__ import annotations

import pytest

from app.reports.model import (
    Align,
    Badge,
    BadgeTone,
    Callout,
    CalloutTone,
    Chart,
    ChartKind,
    ColumnKind,
    Divider,
    Heading,
    KeyValue,
    KeyValueItem,
    Paragraph,
    Report,
    ReportMeta,
    Section,
    Series,
    Spacer,
    Stat,
    Table,
    TableColumn,
    align_of,
    block_from_dict,
)


def _full_report() -> Report:
    return Report(
        meta=ReportMeta(
            title="Test",
            subtitle="Sub",
            kind="quality",
            subject="book-1",
            generated_at="2026-06-28 12:00 UTC",
            footer="ftr",
            tags=("a", "b"),
        ),
        sections=(
            Section(
                title="One",
                page_break_before=True,
                blocks=(
                    Heading("H", level=1),
                    Paragraph("p", muted=True),
                    KeyValue(
                        items=(
                            KeyValueItem("L", Stat(1.0, "1", "s"), emphasis=True),
                            KeyValueItem("M", Stat(2.5)),
                        ),
                        columns=2,
                    ),
                    Table(
                        columns=(
                            TableColumn("a", "A", ColumnKind.TEXT),
                            TableColumn("b", "B", ColumnKind.NUMBER),
                        ),
                        rows=({"a": "x", "b": "1"}, {"a": "y", "b": "2"}),
                        caption="cap",
                        total_row={"a": "T", "b": "3"},
                    ),
                    Chart(
                        kind=ChartKind.BAR,
                        series=(Series("s", (1.0, 2.0), color="#fff"),),
                        labels=("a", "b"),
                        title="ct",
                        height=200,
                        options={"k": "v"},
                    ),
                    Callout("note", tone=CalloutTone.WARNING, title="t"),
                    Badge("PASS", tone=BadgeTone.SUCCESS),
                    Divider(),
                    Spacer(20),
                ),
            ),
        ),
    )


def test_report_round_trips_through_json_losslessly() -> None:
    rep = _full_report()
    assert Report.from_dict(rep.to_dict()).to_dict() == rep.to_dict()


def test_stat_text_prefers_display_then_unit_then_value() -> None:
    assert Stat(1.0, "one").text() == "one"
    assert Stat(2.0, unit="s").text() == "2s"
    assert Stat(3.5).text() == "3.5"


def test_align_of_numbers_right_text_left() -> None:
    assert align_of(ColumnKind.NUMBER) is Align.RIGHT
    assert align_of(ColumnKind.PERCENT) is Align.RIGHT
    assert align_of(ColumnKind.SECONDS) is Align.RIGHT
    assert align_of(ColumnKind.TEXT) is Align.LEFT
    assert align_of(ColumnKind.DATE) is Align.LEFT


def test_table_column_explicit_alignment_wins() -> None:
    col = TableColumn("k", "K", ColumnKind.NUMBER, align=Align.CENTER)
    assert col.alignment() is Align.CENTER


def test_iter_blocks_and_tables_flatten_in_order() -> None:
    rep = _full_report()
    blocks = rep.iter_blocks()
    assert isinstance(blocks[0], Heading)
    assert len(rep.tables()) == 1
    assert rep.tables()[0].caption == "cap"


def test_block_from_dict_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown report block_type"):
        block_from_dict({"block_type": "nope"})


def test_block_from_dict_reconstructs_each_block_kind() -> None:
    rep = _full_report()
    for block in rep.iter_blocks():
        again = block_from_dict(block.to_dict())
        assert again.to_dict() == block.to_dict()
