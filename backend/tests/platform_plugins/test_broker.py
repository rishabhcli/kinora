"""Host-API broker unit tests: gating, metering, ergonomic wrappers."""

from __future__ import annotations

import pytest

from app.platform.plugins.broker import CallMeter, HostAPI, HostServices, maybe_await
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import CapabilityDeniedError, ResourceLimitError


def _api(grants: GrantSet, services: HostServices, *, calls: int = 10, logs_max: int = 10):
    logs: list[str] = []
    meter = CallMeter(max_host_calls=calls, max_log_lines=logs_max)
    api = HostAPI(grants=grants, services=services, meter=meter, logs=logs)
    return api, meter, logs


def test_call_denied_without_grant() -> None:
    ran: list[int] = []
    api, meter, _ = _api(
        GrantSet.of(), HostServices(services={"canon.read": lambda *a: ran.append(1)})
    )
    with pytest.raises(CapabilityDeniedError):
        api.call("canon.read")
    assert ran == []
    assert meter.host_calls == 0  # denial precedes the charge


def test_call_missing_service_is_denied() -> None:
    # Granted but the host doesn't implement it here -> still blocked.
    api, _, _ = _api(GrantSet.of("canon.read"), HostServices(services={}))
    with pytest.raises(CapabilityDeniedError):
        api.call("canon.read")


def test_call_charges_and_records_trail() -> None:
    api, meter, _ = _api(
        GrantSet.of("canon.read"), HostServices(services={"canon.read": lambda *a, **k: "v"})
    )
    assert api.call("canon.read", 1) == "v"
    assert meter.host_calls == 1
    assert meter.trail == ["canon.read"]


def test_host_call_budget() -> None:
    api, _, _ = _api(
        GrantSet.of("canon.read"),
        HostServices(services={"canon.read": lambda *a, **k: 1}),
        calls=2,
    )
    api.call("canon.read")
    api.call("canon.read")
    with pytest.raises(ResourceLimitError) as exc:
        api.call("canon.read")
    assert exc.value.limit == "host_calls"


def test_log_requires_capability_and_charges() -> None:
    api, meter, logs = _api(GrantSet.of("log.write"), HostServices())
    api.log("hello", n=3)
    assert logs == ["hello n=3"]
    assert meter.log_lines == 1


def test_log_denied_without_capability() -> None:
    api, _, _ = _api(GrantSet.of(), HostServices())
    with pytest.raises(CapabilityDeniedError):
        api.log("nope")


def test_log_budget() -> None:
    api, _, _ = _api(GrantSet.of("log.write"), HostServices(), logs_max=1)
    api.log("one")
    with pytest.raises(ResourceLimitError) as exc:
        api.log("two")
    assert exc.value.limit == "log_lines"


def test_permits_is_noncharging() -> None:
    api, meter, _ = _api(GrantSet.of("canon.read"), HostServices())
    assert api.permits("canon.read")
    assert not api.permits("canon.write")
    assert meter.host_calls == 0


def test_wrappers_route_through_call() -> None:
    seen: dict[str, object] = {}

    def _kv_get(k: object) -> str:
        seen["get"] = k
        return "value"

    def _kv_set(k: object, v: object) -> None:
        seen["set"] = (k, v)

    services = HostServices(
        services={
            "storage.kv.read": _kv_get,
            "storage.kv.write": _kv_set,
            "canon.query": lambda beat, **k: {"beat": beat},
            "net.fetch": lambda url, **k: {"url": url},
            "secrets.read": lambda name: f"secret:{name}",
        }
    )
    api, _, _ = _api(
        GrantSet.of(
            "storage.kv.read", "storage.kv.write", "canon.query", "net.fetch", "secrets.read"
        ),
        services,
    )
    assert api.kv_get("k") == "value"
    api.kv_set("k", 1)
    assert seen["set"] == ("k", 1)
    assert api.canon_query("beat_1") == {"beat": "beat_1"}
    assert api.fetch("https://x")["url"] == "https://x"
    assert api.secret("token") == "secret:token"


async def test_maybe_await_passthrough_and_await() -> None:
    assert await maybe_await(5) == 5

    async def _coro() -> int:
        return 7

    assert await maybe_await(_coro()) == 7
