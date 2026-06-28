"""Pure unit tests for the CLI output + formatting layer (no infra).

These exercise the dual-representation rendering contract: every action result
yields a :class:`Payload` that renders identically as a human table or as
machine JSON, and the formatting helpers produce stable, compact strings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.cli.errors import (
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    CliError,
    conflict,
    not_found,
    usage,
)
from app.cli.formatting import (
    ago,
    humanize_bytes,
    humanize_seconds,
    isoformat,
    pct,
    truncate,
    yesno,
)
from app.cli.output import (
    Format,
    Payload,
    Table,
    kv_table,
    render,
    render_json,
    render_table,
)

# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "-"),
        (0, "0s"),
        (5.0, "5s"),
        (4.5, "4.5s"),
        (59.9, "59.9s"),
        (60, "1m"),
        (95, "1m35s"),
        (3725, "1h2m5s"),
        (90061, "1d1h1m1s"),
    ],
)
def test_humanize_seconds(seconds: float | None, expected: str) -> None:
    assert humanize_seconds(seconds) == expected


def test_humanize_seconds_negative() -> None:
    assert humanize_seconds(-5.0).startswith("-")


@pytest.mark.parametrize(
    ("num", "expected"),
    [(None, "-"), (512, "512B"), (1536, "1.5KiB"), (1048576, "1.0MiB")],
)
def test_humanize_bytes(num: int | None, expected: str) -> None:
    assert humanize_bytes(num) == expected


def test_pct() -> None:
    assert pct(1, 4) == "25.0%"
    assert pct(1, 0) == "-"


def test_truncate() -> None:
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 5) == "hell…"
    assert truncate(None) == ""


def test_truncate_strips_newlines() -> None:
    out = truncate("a\nb\rc")
    assert "\n" not in out and "\r" not in out
    assert out == "a b c"


def test_yesno() -> None:
    assert yesno(True) == "yes"
    assert yesno(0) == "no"


def test_ago_and_isoformat() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    past = datetime(2026, 1, 1, 11, 58, 0, tzinfo=UTC)
    assert ago(past, now=now) == "2m ago"
    assert ago(None) == "-"
    assert isoformat(None) is None
    assert isoformat(past) == "2026-01-01T11:58:00+00:00"


def test_ago_naive_datetime_treated_as_utc() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    naive_past = datetime(2026, 1, 1, 11, 59, 0)  # noqa: DTZ001 - intentional
    assert ago(naive_past, now=now) == "1m ago"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_error_exit_codes() -> None:
    assert not_found("book", "b1").exit_code == EXIT_NOT_FOUND
    assert usage("bad").exit_code == EXIT_USAGE
    assert conflict("no").exit_code == EXIT_CONFLICT
    assert CliError("x").exit_code == 1
    assert "book not found: b1" in str(not_found("book", "b1"))


# --------------------------------------------------------------------------- #
# output rendering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FakeResult:
    """A minimal Renderable for the rendering tests."""

    payload: Payload

    def render_payload(self) -> Payload:
        return self.payload


def test_table_render_grid() -> None:
    table = Table(
        title="t",
        columns=("id", "name"),
        rows=[("1", "alpha"), ("22", "b")],
    )
    out = render_table(Payload.of({"x": 1}, table))
    assert "id" in out and "name" in out
    assert "alpha" in out
    # Columns are aligned: the header underline width tracks the widest cell.
    assert "--" in out


def test_table_render_empty_marker() -> None:
    table = Table(title="empties", columns=("a",), rows=[])
    out = render_table(Payload.of({"a": []}, table))
    assert "(empty)" in out


def test_table_falls_back_to_json_without_tables() -> None:
    out = render_table(Payload.of({"k": "v"}))
    assert json.loads(out) == {"k": "v"}


def test_json_render_roundtrips() -> None:
    payload = Payload.of({"a": 1, "nested": {"b": [1, 2, 3]}})
    assert json.loads(render_json(payload)) == {"a": 1, "nested": {"b": [1, 2, 3]}}


def test_render_dispatches_on_format() -> None:
    result = _FakeResult(Payload.of({"k": "v"}, Table("t", ("k",), [("v",)])))
    assert json.loads(render(result, Format.JSON)) == {"k": "v"}
    assert "v" in render(result, Format.TABLE)


def test_kv_table_handles_none_and_objects() -> None:
    table = kv_table("info", {"a": None, "b": 3, "c": "x"})
    assert table.rows == [["a", ""], ["b", "3"], ["c", "x"]]


def test_payload_of_collects_tables() -> None:
    t1 = Table("one", ("a",), [])
    t2 = Table("two", ("b",), [])
    payload = Payload.of({}, t1, t2)
    assert payload.tables == (t1, t2)
