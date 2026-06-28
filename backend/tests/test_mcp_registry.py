"""Unit tests for the MCP tool catalog: versioning + scopes + output schemas (§8.3).

The catalog is the single source of protocol metadata over the §8.3 tool
surface. These tests assert it stays in lock-step with ``TOOL_DEFS`` (the single
execution path) — every tool resolves an output model, a version, and a scope —
and that version resolution behaves (compatible pins pass, incompatible reject).
No infrastructure required.
"""

from __future__ import annotations

import pytest

from app.mcp.registry import Scope, ToolCatalog, ToolVersion, default_catalog
from app.mcp.tools import TOOL_DEFS


def test_catalog_covers_every_tool_def() -> None:
    cat = default_catalog()
    assert set(cat.names()) == {d.name for d in TOOL_DEFS}
    assert len(cat.metas) == len(TOOL_DEFS)


def test_every_tool_has_an_output_model() -> None:
    cat = default_catalog()
    missing = [m.name for m in cat.metas if m.output_model is None]
    assert missing == []


def test_every_tool_has_a_version_and_scope() -> None:
    cat = default_catalog()
    for m in cat.metas:
        assert m.version is not None
        assert m.scopes, f"{m.name} has no scope"


def test_write_and_render_classification() -> None:
    cat = default_catalog()
    # The control-plane render tools are exactly the spend tools.
    render = {m.name for m in cat.with_scope(Scope.RENDER)}
    assert render == {"shot.render", "budget.reserve"}
    # A canon mutation is a write; a read is not.
    assert "canon.upsert_entity" in cat.write_tools()
    assert "canon.query" not in cat.write_tools()
    assert cat.require("episodic.log").is_write
    assert not cat.require("episodic.search").is_write


def test_book_scoped_detection() -> None:
    cat = default_catalog()
    assert "canon.query" in cat.book_scoped_tools()
    # budget.remaining carries no book_id.
    assert "budget.remaining" not in cat.book_scoped_tools()
    # prefs.get has an *optional* book_id field -> still book-scoped by field presence.
    assert cat.require("prefs.get").book_scoped


def test_input_and_output_schemas_are_dicts() -> None:
    cat = default_catalog()
    for m in cat.metas:
        assert isinstance(m.input_schema(), dict)
        out = m.output_schema()
        assert out is None or isinstance(out, dict)


# --- version resolution ------------------------------------------------------


def test_tool_version_parse_and_compat() -> None:
    assert ToolVersion.parse("1.2") == ToolVersion(1, 2)
    assert ToolVersion.parse("3") == ToolVersion(3, 0)
    served = ToolVersion(1, 3)
    assert served.is_compatible_with(ToolVersion(1, 0))  # server has more minors
    assert served.is_compatible_with(ToolVersion(1, 3))
    assert not served.is_compatible_with(ToolVersion(1, 4))  # client wants newer minor
    assert not served.is_compatible_with(ToolVersion(2, 0))  # major mismatch


def test_tool_version_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        ToolVersion.parse("")
    with pytest.raises(ValueError):
        ToolVersion.parse("x.y")


def test_resolve_version_accepts_compatible_and_rejects_incompatible() -> None:
    cat = default_catalog()
    name = "canon.query"
    assert cat.resolve_version(name, None).name == name
    assert cat.resolve_version(name, "1.0").name == name
    with pytest.raises(ValueError):
        cat.resolve_version(name, "2.0")


def test_resolve_version_unknown_tool() -> None:
    cat = default_catalog()
    with pytest.raises(KeyError):
        cat.resolve_version("nope.nope", None)


def test_catalog_is_built_fresh_from_tool_defs() -> None:
    # A catalog built from a slice has only those tools (no hidden global state).
    subset = ToolCatalog.from_tool_defs([TOOL_DEFS[0]])
    assert subset.names() == [TOOL_DEFS[0].name]
