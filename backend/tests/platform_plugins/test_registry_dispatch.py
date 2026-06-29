"""Hook-registry + dispatcher tests: ordering, composition, isolation."""

from __future__ import annotations

from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.hooks import ExtensionPoint, HookSpec
from app.platform.plugins.limits import ResourceLimits
from app.platform.plugins.registry import HookRegistry, RegisteredHook
from app.platform.plugins.runtime import PluginRuntime


def _bind(
    registry: HookRegistry,
    plugin_id: str,
    source: str,
    spec: HookSpec,
    *,
    grants: GrantSet | None = None,
    services: HostServices | None = None,
) -> None:
    rt = PluginRuntime()
    plugin = rt.load(plugin_id=plugin_id, version="1.0.0", source=source)
    registry.register(
        RegisteredHook(
            plugin_id=plugin_id,
            version="1.0.0",
            spec=spec,
            plugin=plugin,
            grants=grants or GrantSet.of(),
            limits=ResourceLimits(),
            services=services or HostServices(),
        )
    )


def test_transform_pipeline_folds_payload() -> None:
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.add",
        "def run(p, host):\n    return p + 1\n",
        HookSpec(id="add", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=10),
    )
    _bind(
        reg,
        "com.a.double",
        "def run(p, host):\n    return p * 2\n",
        HookSpec(id="double", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=20),
    )
    report = reg.dispatch(ExtensionPoint.INGEST_FILTER, 5)
    # priority 10 (add) runs first: (5+1)=6, then double -> 12.
    assert report.payload == 12
    assert report.all_ok


def test_transform_priority_ordering_is_deterministic() -> None:
    reg = HookRegistry()
    # Register out of priority order; dispatch must still run low-priority first.
    _bind(
        reg,
        "com.a.double",
        "def run(p, host):\n    return p * 2\n",
        HookSpec(id="double", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=20),
    )
    _bind(
        reg,
        "com.a.add",
        "def run(p, host):\n    return p + 1\n",
        HookSpec(id="add", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=10),
    )
    report = reg.dispatch(ExtensionPoint.INGEST_FILTER, 5)
    assert report.payload == 12


def test_transform_none_is_passthrough() -> None:
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.noop",
        "def run(p, host):\n    return None\n",
        HookSpec(id="noop", point=ExtensionPoint.INGEST_FILTER, entrypoint="run"),
    )
    report = reg.dispatch(ExtensionPoint.INGEST_FILTER, {"keep": 1})
    assert report.payload == {"keep": 1}  # None did not clobber the payload


def test_produce_collects_values() -> None:
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.one",
        "def run(p, host):\n    return {'tag': 'one'}\n",
        HookSpec(id="h", point=ExtensionPoint.RENDER_POSTPROCESS, entrypoint="run", priority=1),
    )
    _bind(
        reg,
        "com.a.two",
        "def run(p, host):\n    return {'tag': 'two'}\n",
        HookSpec(id="h", point=ExtensionPoint.RENDER_POSTPROCESS, entrypoint="run", priority=2),
    )
    report = reg.dispatch(ExtensionPoint.RENDER_POSTPROCESS, {"shot": "s1"})
    assert report.values == [{"tag": "one"}, {"tag": "two"}]


def test_failing_hook_is_isolated() -> None:
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.bad",
        "def run(p, host):\n    raise ValueError('boom')\n",
        HookSpec(id="bad", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=10),
    )
    _bind(
        reg,
        "com.a.good",
        "def run(p, host):\n    return p + 100\n",
        HookSpec(id="good", point=ExtensionPoint.INGEST_FILTER, entrypoint="run", priority=20),
    )
    report = reg.dispatch(ExtensionPoint.INGEST_FILTER, 1)
    # The bad hook failed but the good hook still ran on the unchanged payload.
    assert report.payload == 101
    assert len(report.failed) == 1
    assert report.failed[0].plugin_id == "com.a.bad"


def test_denied_capability_in_hook_is_isolated_failure() -> None:
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.greedy",
        "def run(p, host):\n    return host.call('canon.write', p)\n",
        HookSpec(id="h", point=ExtensionPoint.RENDER_POSTPROCESS, entrypoint="run"),
        grants=GrantSet.of("canon.read"),  # NOT canon.write
        services=HostServices(services={"canon.write": lambda *a, **k: "ran"}),
    )
    report = reg.dispatch(ExtensionPoint.RENDER_POSTPROCESS, "x")
    assert report.failed
    assert report.failed[0].error_code == "capability_denied"


def test_register_plugin_and_unregister() -> None:
    reg = HookRegistry()
    rt = PluginRuntime()
    plugin = rt.load(
        plugin_id="com.a.multi",
        version="1.0.0",
        source="def a(p, host): return p\ndef b(p, host): return p\n",
    )
    specs = (
        HookSpec(id="a", point=ExtensionPoint.INGEST_FILTER, entrypoint="a"),
        HookSpec(id="b", point=ExtensionPoint.RENDER_POSTPROCESS, entrypoint="b"),
    )
    added = reg.register_plugin(
        plugin=plugin,
        hooks=specs,
        grants=GrantSet.of(),
        limits=ResourceLimits(),
        services=HostServices(),
    )
    assert added == 2
    assert len(reg) == 2
    assert "com.a.multi" in reg.plugin_ids
    removed = reg.unregister_plugin("com.a.multi")
    assert removed == 2
    assert len(reg) == 0


def test_observe_webhook_discards_values() -> None:
    calls: list[str] = []
    reg = HookRegistry()
    _bind(
        reg,
        "com.a.wh",
        "def run(p, host):\n    host.call('net.fetch', p)\n    return 'ignored'\n",
        HookSpec(id="wh", point=ExtensionPoint.WEBHOOK_ACTION, entrypoint="run"),
        grants=GrantSet.of("net.fetch"),
        services=HostServices(services={"net.fetch": lambda url, *a, **k: calls.append(url)}),
    )
    report = reg.dispatch(ExtensionPoint.WEBHOOK_ACTION, "https://hook.example/x")
    assert report.all_ok
    assert calls == ["https://hook.example/x"]
