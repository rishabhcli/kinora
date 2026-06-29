"""Additional sandbox-runtime edge cases beyond the core security proof."""

from __future__ import annotations

import pytest

from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import (
    ForbiddenImportError,
    PluginRuntimeError,
    ResourceLimitError,
)
from app.platform.plugins.limits import ResourceLimits
from app.platform.plugins.runtime import (
    BASE_IMPORT_ALLOWLIST,
    HOST_IMPORT_DENYLIST,
    PluginRuntime,
)


def _invoke(
    source, entrypoint, payload, *, grants=None, services=None, limits=None, allow=frozenset()
):
    rt = PluginRuntime()
    plugin = rt.load(plugin_id="com.t.p", version="1.0.0", source=source, import_allowlist=allow)
    return rt.invoke(
        plugin,
        entrypoint,
        payload,
        grants=grants or GrantSet.of(),
        services=services or HostServices(),
        limits=limits or ResourceLimits(),
    )


def test_syntax_error_at_load_is_wrapped() -> None:
    rt = PluginRuntime()
    with pytest.raises(PluginRuntimeError):
        rt.load(plugin_id="com.t.p", version="1.0.0", source="def x(: pass")


def test_top_level_state_persists_across_invocations() -> None:
    rt = PluginRuntime()
    source = (
        "COUNTER = {'n': 0}\n"
        "def run(p, host):\n"
        "    COUNTER['n'] += 1\n"
        "    return COUNTER['n']\n"
    )
    plugin = rt.load(plugin_id="com.t.p", version="1.0.0", source=source)
    r1 = rt.invoke(
        plugin, "run", None, grants=GrantSet.of(), services=HostServices(), limits=ResourceLimits()
    )
    r2 = rt.invoke(
        plugin, "run", None, grants=GrantSet.of(), services=HostServices(), limits=ResourceLimits()
    )
    assert r1.value == 1
    assert r2.value == 2


def test_invocation_result_metering_fields() -> None:
    source = (
        "def run(p, host):\n"
        "    host.log('a')\n"
        "    host.call('canon.read', 1)\n"
        "    return 'ok'\n"
    )
    result = _invoke(
        source,
        "run",
        None,
        grants=GrantSet.of("log.write", "canon.read"),
        services=HostServices(
            services={"canon.read": lambda *a, **k: 1, "log.write": lambda *a, **k: None}
        ),
    )
    assert result.value == "ok"
    assert result.logs == ["a"]
    assert result.host_calls == 1  # only host.call charges host_calls; log charges log_lines
    assert result.capabilities_used == ("canon.read",)
    assert result.wall_time_ms >= 0.0


def test_unjsonable_output_falls_back_to_repr_size() -> None:
    # An object that isn't JSON-able still gets a size check (via repr) and passes
    # when small.
    source = "def run(p, host):\n    return object()\n"
    result = _invoke(source, "run", None)
    assert result.value is not None


def test_base_and_deny_lists_are_disjoint() -> None:
    # Sanity: nothing in the base allowlist is on the host denylist.
    assert not (BASE_IMPORT_ALLOWLIST & HOST_IMPORT_DENYLIST)


def test_submodule_of_allowed_package_importable() -> None:
    # collections.abc is in the base allowlist (explicitly); importing it works.
    source = "from collections import abc\ndef run(p, host):\n    return hasattr(abc, 'Mapping')\n"
    result = _invoke(source, "run", None)
    assert result.value is True


def test_import_of_denied_submodule_blocked() -> None:
    source = "def run(p, host):\n    import os.path\n    return 1\n"
    with pytest.raises(ForbiddenImportError):
        _invoke(source, "run", None)


def test_zero_wall_time_is_rejected_by_limits() -> None:
    # Resource limits validate positivity.
    from app.platform.plugins.errors import PluginValidationError

    with pytest.raises(PluginValidationError):
        ResourceLimits(wall_time_ms=0)


def test_output_size_enforced_on_large_dict() -> None:
    source = "def run(p, host):\n    return {'k': 'v' * 10000}\n"
    with pytest.raises(ResourceLimitError):
        _invoke(source, "run", None, limits=ResourceLimits(max_output_bytes=500))
