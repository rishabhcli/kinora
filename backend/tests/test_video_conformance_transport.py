"""Unit tests for the scripted fake transport + the protocol/capabilities models."""

from __future__ import annotations

import pytest

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)
from app.providers.types import WanMode
from app.video.conformance.protocol import (
    DurationBounds,
    SubmittedTask,
    TaskStatus,
    VideoCapabilities,
    all_modes,
    provider_surface,
)
from app.video.conformance.transport import (
    Fault,
    ScriptedTransport,
    TransportScript,
    error_for_fault,
)

# --------------------------------------------------------------------------- #
# Fault → taxonomy mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fault", "cls", "retryable"),
    [
        (Fault.BAD_REQUEST, ProviderBadRequest, False),
        (Fault.AUTH, AuthenticationError, False),
        (Fault.RATE_LIMITED, RateLimited, True),
        (Fault.TRANSIENT, TransientProviderError, True),
        (Fault.TIMEOUT, ProviderTimeout, True),
    ],
)
def test_error_for_fault_maps_to_taxonomy(
    fault: Fault, cls: type[ProviderError], retryable: bool
) -> None:
    err = error_for_fault(fault)
    assert isinstance(err, cls)
    assert err.retryable is retryable


def test_error_for_fault_none_raises() -> None:
    with pytest.raises(ValueError):
        error_for_fault(Fault.NONE)


def test_rate_limited_carries_retry_after() -> None:
    err = error_for_fault(Fault.RATE_LIMITED)
    assert isinstance(err, RateLimited)
    assert err.retry_after_s == 1.0
    assert err.status_code == 429


# --------------------------------------------------------------------------- #
# Lifecycle: submit → poll → succeed → download
# --------------------------------------------------------------------------- #


def test_happy_lifecycle() -> None:
    t = ScriptedTransport(TransportScript.healthy())
    task_id = t.submit({"model": "x", "prompt": "hi"})
    assert t.poll(task_id) == "succeeded"
    url = t.clip_url(task_id)
    assert url.startswith("https://")
    assert t.download(url)  # real bytes
    assert t.fetch_calls == 1
    assert t.last_submit_body == {"model": "x", "prompt": "hi"}


def test_multi_tick_lifecycle() -> None:
    # ticks_to_done=N → the task succeeds on the Nth poll.
    t = ScriptedTransport(TransportScript(ticks_to_done=3))
    task_id = t.submit({"model": "x"})
    assert t.poll(task_id) == "running"
    assert t.poll(task_id) == "running"
    assert t.poll(task_id) == "succeeded"
    # Polling a terminal task is idempotent.
    assert t.poll(task_id) == "succeeded"


def test_never_completes_stays_running() -> None:
    t = ScriptedTransport(TransportScript.never_completes())
    task_id = t.submit({"model": "x"})
    for _ in range(50):
        assert t.poll(task_id) in ("pending", "running")


def test_submit_fault_consumed_in_order() -> None:
    t = ScriptedTransport(TransportScript.with_submit_faults([Fault.RATE_LIMITED, Fault.NONE]))
    with pytest.raises(RateLimited):
        t.submit({"model": "x"})
    # The next submit (NONE) succeeds.
    assert t.submit({"model": "x"})


def test_poll_fault() -> None:
    t = ScriptedTransport(TransportScript.with_poll_faults([Fault.TRANSIENT]))
    task_id = t.submit({"model": "x"})
    with pytest.raises(TransientProviderError):
        t.poll(task_id)


def test_idempotent_submit_dedupes_by_shot_id() -> None:
    t = ScriptedTransport(TransportScript.healthy())
    a = t.submit({"model": "x"}, shot_id="s1")
    b = t.submit({"model": "x"}, shot_id="s1")
    assert a == b
    assert t.submit_calls == 2  # two calls reached the transport
    # But only one task was minted.
    assert len(t._tasks) == 1  # noqa: SLF001 - white-box assertion of no double-spend


def test_cancel_moves_to_canceled() -> None:
    t = ScriptedTransport(TransportScript(ticks_to_done=5))
    task_id = t.submit({"model": "x"})
    t.cancel(task_id)
    assert t.cancel_calls == 1
    assert t.poll(task_id) == "canceled"


def test_clip_url_before_success_raises() -> None:
    t = ScriptedTransport(TransportScript(ticks_to_done=5))
    task_id = t.submit({"model": "x"})
    with pytest.raises(ProviderError):
        t.clip_url(task_id)


def test_unknown_task_is_bad_request() -> None:
    t = ScriptedTransport()
    with pytest.raises(ProviderBadRequest):
        t.poll("nope")


# --------------------------------------------------------------------------- #
# DurationBounds + VideoCapabilities
# --------------------------------------------------------------------------- #


def test_duration_bounds_contains_and_edges() -> None:
    b = DurationBounds(min_s=5, max_s=15)
    assert b.contains(5) and b.contains(10) and b.contains(15)
    assert not b.contains(4) and not b.contains(16)
    assert b.just_below() == 4
    assert b.just_above() == 16
    assert 5 <= b.representative_inside() <= 15


def test_duration_bounds_min_one_has_no_just_below() -> None:
    b = DurationBounds(min_s=1, max_s=5)
    assert b.just_below() is None


@pytest.mark.parametrize(("lo", "hi"), [(0, 5), (5, 4), (-1, 3)])
def test_duration_bounds_reject_invalid(lo: int, hi: int) -> None:
    with pytest.raises(ValueError):
        DurationBounds(min_s=lo, max_s=hi)


def test_capabilities_example_helpers_are_deterministic() -> None:
    caps = VideoCapabilities(
        provider_id="p",
        modes=frozenset({WanMode.IMAGE_TO_VIDEO, WanMode.TEXT_TO_VIDEO}),
        resolutions=frozenset({"720P", "480P"}),
    )
    # Lowest-valued mode + lexicographically-first resolution.
    assert caps.example_mode() == min(caps.modes, key=lambda m: m.value)
    assert caps.example_resolution() == "480P"
    assert caps.supports_mode(WanMode.TEXT_TO_VIDEO)
    assert not caps.supports_mode(WanMode.REFERENCE_TO_VIDEO)
    assert caps.supports_resolution("720P")
    assert not caps.supports_resolution("1080P")


def test_capabilities_empty_modes_raise_on_example() -> None:
    caps = VideoCapabilities(provider_id="p", modes=frozenset())
    with pytest.raises(ValueError):
        caps.example_mode()


def test_all_modes_covers_enum() -> None:
    assert set(all_modes()) == set(WanMode)


# --------------------------------------------------------------------------- #
# provider_surface + lifecycle status helpers
# --------------------------------------------------------------------------- #


def test_provider_surface_detects_members() -> None:
    class Full:
        name = "f"

        def capabilities(self) -> None: ...
        async def render(self, spec: object) -> None: ...
        async def submit(self, spec: object) -> None: ...
        async def poll(self, t: object) -> None: ...
        async def fetch(self, t: object) -> None: ...
        async def cancel(self, t: object) -> None: ...

    surface = provider_surface(Full())
    assert surface.has_render and surface.has_capabilities
    assert surface.has_staged_lifecycle and surface.has_cancel

    class OneShot:
        name = "o"

        def capabilities(self) -> None: ...
        async def render(self, spec: object) -> None: ...

    surface = provider_surface(OneShot())
    assert surface.has_render and not surface.has_submit
    assert not surface.has_staged_lifecycle


def test_task_status_terminality() -> None:
    assert TaskStatus(task_id="t", state="succeeded").is_success
    assert TaskStatus(task_id="t", state="succeeded").is_terminal
    assert TaskStatus(task_id="t", state="failed").is_terminal
    assert not TaskStatus(task_id="t", state="running").is_terminal
    assert not TaskStatus(task_id="t", state="running").is_success


def test_submitted_task_carries_identity() -> None:
    task = SubmittedTask(task_id="task-1", model="m", shot_id="s")
    assert task.task_id == "task-1"
    assert task.model == "m"
    assert task.shot_id == "s"
