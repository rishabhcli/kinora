"""Dependency-resolution unit tests: ranges, closure, conflicts, cycles, order."""

from __future__ import annotations

import pytest

from app.platform.plugins.errors import DependencyResolutionError
from app.platform.plugins.manifest import PluginManifest
from app.platform.plugins.resolver import (
    AvailablePlugin,
    DependencyResolver,
    _topological_sort,
)
from app.platform.plugins.version import Version


def _manifest(pid: str, version: str, deps: list[dict] | None = None) -> PluginManifest:
    return PluginManifest.parse(
        {
            "id": pid,
            "version": version,
            "name": pid,
            "capabilities": ["log.write"],
            "hooks": [{"id": "h", "point": "ingest.filter", "entrypoint": "run"}],
            "dependencies": deps or [],
        }
    )


def _avail(
    pid: str, version: str, deps: list[dict] | None = None, enabled: bool = True
) -> AvailablePlugin:
    m = _manifest(pid, version, deps)
    return AvailablePlugin(
        id=pid, version=Version.parse(version), dependencies=m.dependencies, enabled=enabled
    )


def test_resolve_no_dependencies() -> None:
    target = _manifest("com.a.app", "1.0.0")
    res = DependencyResolver([]).resolve(target)
    assert res.order == ["com.a.app"]
    assert res.chosen["com.a.app"] == Version.parse("1.0.0")


def test_resolve_picks_highest_matching_version() -> None:
    available = [
        _avail("com.a.base", "1.0.0"),
        _avail("com.a.base", "1.4.0"),
        _avail("com.a.base", "2.0.0"),
    ]
    target = _manifest("com.a.app", "1.0.0", [{"plugin_id": "com.a.base", "range": "^1.0"}])
    res = DependencyResolver(available).resolve(target)
    assert res.chosen["com.a.base"] == Version.parse("1.4.0")  # highest in ^1


def test_topological_order_deps_before_dependents() -> None:
    available = [
        _avail("com.a.base", "1.0.0"),
        _avail("com.a.mid", "1.0.0", [{"plugin_id": "com.a.base", "range": "*"}]),
    ]
    target = _manifest("com.a.app", "1.0.0", [{"plugin_id": "com.a.mid", "range": "*"}])
    res = DependencyResolver(available).resolve(target)
    assert res.order.index("com.a.base") < res.order.index("com.a.mid")
    assert res.order.index("com.a.mid") < res.order.index("com.a.app")


def test_missing_required_dependency_raises() -> None:
    target = _manifest("com.a.app", "1.0.0", [{"plugin_id": "com.a.missing", "range": "*"}])
    with pytest.raises(DependencyResolutionError, match="not installed"):
        DependencyResolver([]).resolve(target)


def test_unsatisfiable_range_raises() -> None:
    available = [_avail("com.a.base", "1.0.0")]
    target = _manifest("com.a.app", "1.0.0", [{"plugin_id": "com.a.base", "range": ">=2.0.0"}])
    with pytest.raises(DependencyResolutionError, match="satisfies"):
        DependencyResolver(available).resolve(target)


def test_disabled_dependency_raises() -> None:
    available = [_avail("com.a.base", "1.0.0", enabled=False)]
    target = _manifest("com.a.app", "1.0.0", [{"plugin_id": "com.a.base", "range": "*"}])
    with pytest.raises(DependencyResolutionError, match="disabled"):
        DependencyResolver(available).resolve(target)


def test_optional_dependency_skipped_when_absent() -> None:
    target = _manifest(
        "com.a.app",
        "1.0.0",
        [{"plugin_id": "com.a.opt", "range": "*", "optional": True}],
    )
    res = DependencyResolver([]).resolve(target)
    assert "com.a.opt" in res.skipped_optional
    assert res.order == ["com.a.app"]


def test_version_conflict_raises() -> None:
    # app needs base ^1, but mid needs base ^2 -> no single version satisfies both.
    available = [
        _avail("com.a.base", "1.0.0"),
        _avail("com.a.base", "2.0.0"),
        _avail("com.a.mid", "1.0.0", [{"plugin_id": "com.a.base", "range": "^2.0"}]),
    ]
    target = _manifest(
        "com.a.app",
        "1.0.0",
        [
            {"plugin_id": "com.a.base", "range": "^1.0"},
            {"plugin_id": "com.a.mid", "range": "*"},
        ],
    )
    with pytest.raises(DependencyResolutionError, match="conflict"):
        DependencyResolver(available).resolve(target)


def test_cycle_detected() -> None:
    edges = {"a": {"b"}, "b": {"a"}}
    with pytest.raises(DependencyResolutionError, match="cycle"):
        _topological_sort(edges)


def test_topological_sort_is_deterministic() -> None:
    edges = {"app": {"x", "y"}, "x": {"base"}, "y": {"base"}, "base": set()}
    order = _topological_sort(edges)
    assert order[0] == "base"
    assert order[-1] == "app"
    # ties resolved alphabetically: x before y.
    assert order.index("x") < order.index("y")
