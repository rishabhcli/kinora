"""Deterministic proof that the sandbox blocks disallowed capabilities.

These are the security-critical tests the task calls for. They are pure and
infra-free: an in-memory :class:`HostServices` records whether a host function
ran, and we assert that a *denied* capability raises **before** the host
function is touched (no side effect), while a *granted* one runs exactly once
and is charged to the budget.
"""

from __future__ import annotations

import pytest

from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import (
    CapabilityDeniedError,
    ForbiddenImportError,
    PluginRuntimeError,
    ResourceLimitError,
)
from app.platform.plugins.limits import ResourceLimits
from app.platform.plugins.runtime import PluginRuntime


class _Recorder:
    """A host service that records every call so tests can assert side effects."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "host-ran"


def _load(source: str, *, allow: frozenset[str] = frozenset()) -> object:
    rt = PluginRuntime()
    return rt.load(plugin_id="com.test.p", version="1.0.0", source=source, import_allowlist=allow)


def _invoke(
    source: str,
    entrypoint: str,
    payload: object,
    *,
    grants: GrantSet,
    services: HostServices,
    limits: ResourceLimits | None = None,
    allow: frozenset[str] = frozenset(),
):
    rt = PluginRuntime()
    plugin = rt.load(plugin_id="com.test.p", version="1.0.0", source=source, import_allowlist=allow)
    return rt.invoke(
        plugin,
        entrypoint,
        payload,
        grants=grants,
        services=services,
        limits=limits or ResourceLimits(),
    )


# --------------------------------------------------------------------------- #
# Capability denial — the central guarantee
# --------------------------------------------------------------------------- #


def test_denied_capability_raises_before_side_effect() -> None:
    recorder = _Recorder()
    services = HostServices(services={"canon.write": recorder})
    source = "def run(payload, host):\n    return host.call('canon.write', payload)\n"

    # The plugin is granted ONLY canon.read — calling canon.write must be denied.
    with pytest.raises(CapabilityDeniedError) as exc:
        _invoke(source, "run", "x", grants=GrantSet.of("canon.read"), services=services)

    assert exc.value.capability == "canon.write"
    # The host function must NEVER have executed — denial precedes side effect.
    assert recorder.calls == []


def test_granted_capability_runs_exactly_once_and_is_charged() -> None:
    recorder = _Recorder()
    services = HostServices(services={"canon.query": recorder})
    source = "def run(payload, host):\n    return host.call('canon.query', beat_id=payload)\n"

    result = _invoke(source, "run", "beat_1", grants=GrantSet.of("canon.query"), services=services)

    assert result.value == "host-ran"
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1] == {"beat_id": "beat_1"}
    assert result.host_calls == 1
    assert result.capabilities_used == ("canon.query",)


def test_parent_grant_covers_child_capability() -> None:
    recorder = _Recorder()
    services = HostServices(services={"canon.write": recorder})
    source = "def run(payload, host):\n    return host.call('canon.write', payload)\n"

    # Granting the parent 'canon' must permit the child 'canon.write'.
    result = _invoke(source, "run", "x", grants=GrantSet.of("canon"), services=services)
    assert result.value == "host-ran"
    assert len(recorder.calls) == 1


def test_empty_grants_deny_everything() -> None:
    recorder = _Recorder()
    services = HostServices(services={"log.write": recorder})
    source = "def run(payload, host):\n    host.log('hello')\n    return 1\n"

    with pytest.raises(CapabilityDeniedError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=services)


def test_permits_predicate_does_not_charge_budget() -> None:
    services = HostServices(services={})
    source = (
        "def run(payload, host):\n"
        "    return host.permits('net.fetch'), host.permits('canon.read')\n"
    )
    result = _invoke(source, "run", None, grants=GrantSet.of("canon.read"), services=services)
    assert result.value == (False, True)
    assert result.host_calls == 0  # permits() is non-charging


# --------------------------------------------------------------------------- #
# Import allowlist — no ambient filesystem / network / interpreter access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "socket", "pathlib", "app"])
def test_forbidden_import_blocked_at_call_time(module: str) -> None:
    source = f"def run(payload, host):\n    import {module}\n    return True\n"
    with pytest.raises(ForbiddenImportError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())


def test_forbidden_import_blocked_at_load_time() -> None:
    # A malicious top-level import is blocked when the module body executes.
    source = "import os\ndef run(payload, host):\n    return True\n"
    with pytest.raises(ForbiddenImportError):
        _load(source)


def test_denylisted_module_blocked_even_if_requested() -> None:
    # Requesting 'os' in the manifest allowlist does NOT grant it — denylist wins.
    source = "def run(payload, host):\n    import os\n    return os.getcwd()\n"
    with pytest.raises(ForbiddenImportError):
        _invoke(
            source,
            "run",
            None,
            grants=GrantSet.of(),
            services=HostServices(),
            allow=frozenset({"os"}),
        )


def test_allowed_stdlib_import_works() -> None:
    source = "import math\n" "def run(payload, host):\n" "    return math.floor(payload)\n"
    result = _invoke(source, "run", 3.7, grants=GrantSet.of(), services=HostServices())
    assert result.value == 3


def test_manifest_allowlist_widens_imports() -> None:
    # 'calendar' is NOT in the base allowlist: blocked by default, allowed only
    # once the manifest declares it (and it is not on the host denylist).
    source = "import calendar\ndef run(p, host):\n    return calendar.isleap(2024)\n"
    with pytest.raises(ForbiddenImportError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())
    result = _invoke(
        source,
        "run",
        None,
        grants=GrantSet.of(),
        services=HostServices(),
        allow=frozenset({"calendar"}),
    )
    assert result.value is True


def test_relative_import_is_forbidden() -> None:
    # Cannot escape via importlib-style relative import.
    source = "def run(payload, host):\n" "    return __import__('x', globals(), locals(), (), 1)\n"
    with pytest.raises((ForbiddenImportError, CapabilityDeniedError, PluginRuntimeError)):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())


# --------------------------------------------------------------------------- #
# Dangerous builtins are absent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["open", "eval", "exec", "compile", "input", "globals"])
def test_dangerous_builtin_absent(name: str) -> None:
    source = f"def run(payload, host):\n    return {name}\n"
    with pytest.raises(PluginRuntimeError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())


def test_cannot_open_a_file() -> None:
    source = "def run(payload, host):\n    return open('/etc/passwd').read()\n"
    with pytest.raises(PluginRuntimeError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())


def test_safe_builtins_still_available() -> None:
    source = (
        "def run(payload, host):\n"
        "    return sorted(set([3, 1, 2, 1])), len('abc'), sum(range(4))\n"
    )
    result = _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())
    assert result.value == ([1, 2, 3], 3, 6)


# --------------------------------------------------------------------------- #
# Resource budgets
# --------------------------------------------------------------------------- #


def test_host_call_budget_exhaustion() -> None:
    recorder = _Recorder()
    services = HostServices(services={"canon.read": recorder})
    source = (
        "def run(payload, host):\n"
        "    for _ in range(10):\n"
        "        host.call('canon.read', 1)\n"
        "    return 'done'\n"
    )
    limits = ResourceLimits(max_host_calls=3)
    with pytest.raises(ResourceLimitError) as exc:
        _invoke(
            source, "run", None, grants=GrantSet.of("canon.read"), services=services, limits=limits
        )
    assert exc.value.limit == "host_calls"
    # The 4th call must not have executed the host function (budget tripped first).
    assert len(recorder.calls) == 3


def test_wall_time_budget_exhaustion() -> None:
    source = "def run(payload, host):\n" "    x = 0\n" "    while True:\n" "        x += 1\n"
    limits = ResourceLimits(wall_time_ms=50)
    with pytest.raises(ResourceLimitError) as exc:
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices(), limits=limits)
    assert exc.value.limit == "wall_time"


def test_output_size_budget() -> None:
    source = "def run(payload, host):\n    return 'x' * 5000\n"
    limits = ResourceLimits(max_output_bytes=1000)
    with pytest.raises(ResourceLimitError) as exc:
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices(), limits=limits)
    assert exc.value.limit == "output_bytes"


def test_log_line_budget() -> None:
    source = (
        "def run(payload, host):\n"
        "    for i in range(10):\n"
        "        host.log('line', i=i)\n"
        "    return 1\n"
    )
    limits = ResourceLimits(max_log_lines=3)
    with pytest.raises(ResourceLimitError) as exc:
        _invoke(
            source,
            "run",
            None,
            grants=GrantSet.of("log.write"),
            services=HostServices(),
            limits=limits,
        )
    assert exc.value.limit == "log_lines"


# --------------------------------------------------------------------------- #
# Uncaught plugin exceptions are sanitized
# --------------------------------------------------------------------------- #


def test_plugin_exception_is_wrapped() -> None:
    source = "def run(payload, host):\n    raise ValueError('boom secret detail')\n"
    with pytest.raises(PluginRuntimeError) as exc:
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())
    # The raw message does not leak into the wrapped error's str (only the type).
    assert "ValueError" in str(exc.value)


def test_missing_entrypoint_raises() -> None:
    source = "def other(payload, host):\n    return 1\n"
    with pytest.raises(PluginRuntimeError):
        _invoke(source, "run", None, grants=GrantSet.of(), services=HostServices())
