"""Sandbox: capability allow-list, no ambient creds, budgets, error containment."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.video.plugins.contracts import (
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoRequest,
)
from app.video.plugins.errors import (
    AmbientCredentialError,
    CapabilityDeniedError,
    PluginRuntimeError,
    ResourceLimitError,
)
from app.video.plugins.limits import ResourceLimits
from app.video.plugins.sandbox import (
    CapabilityGrant,
    HostServices,
    Sandbox,
)

_PROFILE = CapabilityProfile(modes=frozenset({RenderMode.TEXT_TO_VIDEO}))


def _t2v_request() -> VideoRequest:
    return VideoRequest(mode=RenderMode.TEXT_TO_VIDEO, prompt="x", duration_s=2.0)


def _sandbox(
    *,
    grant: CapabilityGrant,
    services: HostServices | None = None,
    limits: ResourceLimits | None = None,
) -> Sandbox:
    return Sandbox(
        plugin_id="com.test.p",
        grant=grant,
        services=services or HostServices(),
        limits=limits or ResourceLimits(),
    )


# --- capability allow-list ------------------------------------------------- #


def test_grant_rejects_unknown_capability() -> None:
    with pytest.raises(CapabilityDeniedError):
        CapabilityGrant.from_iterable(["net.fetch", "totally.bogus"])


async def test_ungranted_capability_denied_before_side_effect() -> None:
    calls: list[Any] = []

    async def fetch(*a: Any, **k: Any) -> str:
        calls.append((a, k))
        return "should-not-run"

    sandbox = _sandbox(grant=CapabilityGrant(frozenset()), services=HostServices(fetch=fetch))
    handle, _meter = sandbox.make_handle()
    with pytest.raises(CapabilityDeniedError) as exc:
        await handle.fetch("GET", "https://x")
    assert exc.value.capability == "net.fetch"
    assert calls == []  # the host fetch never ran


async def test_granted_capability_allows_call() -> None:
    async def fetch(method: str, url: str) -> dict[str, str]:
        return {"method": method, "url": url}

    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset({"net.fetch"})), services=HostServices(fetch=fetch)
    )
    handle, _ = sandbox.make_handle()
    assert await handle.fetch("GET", "https://x") == {"method": "GET", "url": "https://x"}


# --- no ambient credentials ------------------------------------------------ #


def test_undeclared_secret_raises_ambient_credential_error() -> None:
    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset({"host.secret"})),
        services=HostServices(secrets={"api_key": "sk-1"}),
    )
    handle, _ = sandbox.make_handle()
    assert handle.host_secret("api_key") == "sk-1"
    with pytest.raises(AmbientCredentialError) as exc:
        handle.host_secret("OPENAI_API_KEY")
    assert exc.value.name == "OPENAI_API_KEY"


def test_secret_requires_capability() -> None:
    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset()), services=HostServices(secrets={"k": "v"})
    )
    handle, _ = sandbox.make_handle()
    with pytest.raises(CapabilityDeniedError):
        handle.host_secret("k")


def test_handle_exposes_no_ambient_state() -> None:
    sandbox = _sandbox(grant=CapabilityGrant(frozenset({"host.secret"})))
    handle, _ = sandbox.make_handle()
    # The handle carries no settings / env / global-store attribute.
    public = [a for a in dir(handle) if not a.startswith("_")]
    assert set(public) == {"fetch", "host_secret", "log", "report_usage"}


# --- host-call budget ------------------------------------------------------ #


async def test_host_call_budget_enforced() -> None:
    async def fetch(*a: Any, **k: Any) -> int:
        return 1

    limits = ResourceLimits(max_host_calls=2)
    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset({"net.fetch"})),
        services=HostServices(fetch=fetch),
        limits=limits,
    )
    handle, meter = sandbox.make_handle()
    await handle.fetch()
    await handle.fetch()
    with pytest.raises(ResourceLimitError) as exc:
        await handle.fetch()
    assert exc.value.limit == "host_calls"
    assert meter.host_calls == 3


# --- wall-time guard ------------------------------------------------------- #


class SlowPlugin:
    capabilities = _PROFILE

    async def probe(self) -> ProbeResult:
        await asyncio.sleep(5)
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        await asyncio.sleep(5)
        return VideoArtifact(
            clip_url="x", duration_s=1.0, model="m", mode=request.mode
        )


async def test_wall_time_budget_trips() -> None:
    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset()), limits=ResourceLimits(wall_time_ms=20)
    )
    with pytest.raises(ResourceLimitError) as exc:
        await sandbox.generate(SlowPlugin(), _t2v_request())
    assert exc.value.limit == "wall_time"


# --- error containment ----------------------------------------------------- #


class ExplodingPlugin:
    capabilities = _PROFILE

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        raise ValueError("kaboom from third-party code")


async def test_plugin_exception_contained_as_typed_error() -> None:
    sandbox = _sandbox(grant=CapabilityGrant(frozenset()))
    with pytest.raises(PluginRuntimeError) as exc:
        await sandbox.generate(ExplodingPlugin(), _t2v_request())
    # The raw ValueError never escapes; its repr is preserved for the log.
    assert "ValueError" in (exc.value.original or "")
    assert isinstance(exc.value.__cause__, ValueError)


class WrongTypePlugin:
    capabilities = _PROFILE

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> Any:
        return {"not": "a VideoArtifact"}


async def test_wrong_return_type_contained() -> None:
    sandbox = _sandbox(grant=CapabilityGrant(frozenset()))
    with pytest.raises(PluginRuntimeError):
        await sandbox.generate(WrongTypePlugin(), _t2v_request())


def test_factory_exception_contained() -> None:
    def bad_factory(*, config: dict[str, Any], host: object) -> Any:
        raise RuntimeError("constructor blew up")

    sandbox = _sandbox(grant=CapabilityGrant(frozenset()))
    handle, _ = sandbox.make_handle()
    with pytest.raises(PluginRuntimeError):
        Sandbox.instantiate(bad_factory, config={}, host=handle)


def test_factory_returning_non_plugin_contained() -> None:
    def not_a_plugin(*, config: dict[str, Any], host: object) -> Any:
        return object()

    sandbox = _sandbox(grant=CapabilityGrant(frozenset()))
    handle, _ = sandbox.make_handle()
    with pytest.raises(PluginRuntimeError):
        Sandbox.instantiate(not_a_plugin, config={}, host=handle)


# --- output-size guard ----------------------------------------------------- #


class HugeOutputPlugin:
    capabilities = _PROFILE

    async def probe(self) -> ProbeResult:
        return ProbeResult(healthy=True)

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        return VideoArtifact(
            clip_url="https://x/" + ("a" * 5000),
            duration_s=1.0,
            model="m",
            mode=request.mode,
        )


async def test_output_size_budget_trips() -> None:
    sandbox = _sandbox(
        grant=CapabilityGrant(frozenset()), limits=ResourceLimits(max_output_bytes=256)
    )
    with pytest.raises(ResourceLimitError) as exc:
        await sandbox.generate(HugeOutputPlugin(), _t2v_request())
    assert exc.value.limit == "output_bytes"
