"""Unit tests for the audio router: capability-aware selection, health-based
failover (pure circuit breaker), the async-job seam, and inline-timestamp
preference. Deterministic fakes only — no network, no spend."""

from __future__ import annotations

import pytest

from app.audio.protocol import AudioJob, JobStatus, OneShotAudioProvider
from app.audio.router import (
    AudioRouter,
    AudioRouterPolicy,
    BackendHealth,
    BackendStatus,
    can_serve,
)
from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioResult,
    AudioTrackType,
    AudioWord,
    TtsRequest,
)
from app.providers.errors import ProviderBadRequest, TransientProviderError


def _cap(provider: str, **kw: object) -> AudioCapability:
    base: dict[str, object] = {
        "provider": provider,
        "model": f"{provider}-1",
        "track_types": frozenset({AudioTrackType.NARRATION}),
    }
    base.update(kw)
    return AudioCapability(**base)


def _result(provider: str) -> AudioResult:
    return AudioResult(
        audio_bytes=b"WAV-" + provider.encode(),
        sample_rate=24000,
        duration_s=1.0,
        model=f"{provider}-1",
        provider=provider,
        word_timestamps=(AudioWord(text="hi", t_start=0.0, t_end=0.5),),
        alignment=AlignmentMethod.PROPORTIONAL,
    )


class FakeAudioBackend(OneShotAudioProvider):
    """A scriptable UniversalAudioProvider: each synth pops the next action."""

    def __init__(
        self,
        provider: str,
        *,
        capability: AudioCapability | None = None,
        script: list[object] | None = None,
        healthy: bool = True,
    ) -> None:
        self.name = f"audio:{provider}"
        self._provider = provider
        self._cap = capability or _cap(provider)
        self._script = list(script or [])
        self._healthy = healthy
        self.calls = 0
        self._oneshot_cache: dict[str, AudioResult] = {}

    def capabilities(self) -> AudioCapability:
        return self._cap

    async def healthy(self) -> bool:
        return self._healthy

    async def synthesize(self, request: TtsRequest) -> AudioResult:
        self.calls += 1
        action = self._script.pop(0) if self._script else _result(self._provider)
        if isinstance(action, Exception):
            raise action
        return action  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# can_serve pre-filter
# --------------------------------------------------------------------------- #


def test_can_serve_gates_on_capability() -> None:
    cap = _cap("p", voices=("Cherry",), languages=("en-US",), max_input_chars=5)
    assert can_serve(cap, TtsRequest(text="hi", voice_id="Cherry", language="en-US"))
    assert not can_serve(cap, TtsRequest(text="hi", voice_id="Unknown"))
    assert not can_serve(cap, TtsRequest(text="hi", language="fr-FR"))
    assert not can_serve(cap, TtsRequest(text="toolong"))
    assert not can_serve(cap, TtsRequest(text="hi", is_ssml=True))


# --------------------------------------------------------------------------- #
# Selection + failover
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_router_routes_to_preferred() -> None:
    a = FakeAudioBackend("a")
    b = FakeAudioBackend("b")
    router = AudioRouter([a, b])
    res = await router.synthesize(TtsRequest(text="hi"))
    assert res.provider == "a"
    assert a.calls == 1 and b.calls == 0


@pytest.mark.asyncio
async def test_router_skips_incapable_backend() -> None:
    # 'a' cannot do the requested voice; 'b' can (open set).
    a = FakeAudioBackend("a", capability=_cap("a", voices=("Cherry",)))
    b = FakeAudioBackend("b")
    router = AudioRouter([a, b])
    res = await router.synthesize(TtsRequest(text="hi", voice_id="custom"))
    assert res.provider == "b"
    assert a.calls == 0


@pytest.mark.asyncio
async def test_router_fails_over_on_retryable() -> None:
    a = FakeAudioBackend("a", script=[TransientProviderError("blip")])
    b = FakeAudioBackend("b")
    router = AudioRouter([a, b])
    res = await router.synthesize(TtsRequest(text="hi"))
    assert res.provider == "b"
    assert a.calls == 1 and b.calls == 1
    assert router.health("audio:a").total_failures == 1


@pytest.mark.asyncio
async def test_router_short_circuits_non_retryable() -> None:
    a = FakeAudioBackend("a", script=[ProviderBadRequest("bad spec")])
    b = FakeAudioBackend("b")
    router = AudioRouter([a, b])
    with pytest.raises(ProviderBadRequest):
        await router.synthesize(TtsRequest(text="hi"))
    assert b.calls == 0  # never tried — same request fails everywhere


@pytest.mark.asyncio
async def test_router_no_capable_backend_raises_value_error() -> None:
    a = FakeAudioBackend("a", capability=_cap("a", voices=("Cherry",)))
    router = AudioRouter([a])
    with pytest.raises(ValueError, match="no audio backend"):
        await router.synthesize(TtsRequest(text="hi", voice_id="missing"))


@pytest.mark.asyncio
async def test_router_raises_last_retryable_when_all_fail() -> None:
    a = FakeAudioBackend("a", script=[TransientProviderError("a-down")])
    b = FakeAudioBackend("b", script=[TransientProviderError("b-down")])
    router = AudioRouter([a, b])
    with pytest.raises(TransientProviderError, match="b-down"):
        await router.synthesize(TtsRequest(text="hi"))


@pytest.mark.asyncio
async def test_router_prefers_inline_timestamps() -> None:
    no_ts = FakeAudioBackend("a", capability=_cap("a", supports_word_timestamps=False))
    with_ts = FakeAudioBackend("b", capability=_cap("b", supports_word_timestamps=True))
    router = AudioRouter(
        [no_ts, with_ts], policy=AudioRouterPolicy(prefer_inline_timestamps=True)
    )
    res = await router.synthesize(TtsRequest(text="hi", word_timestamps=True))
    # 'b' is preferred despite being second in priority order.
    assert res.provider == "b"


@pytest.mark.asyncio
async def test_router_construction_requires_backend() -> None:
    with pytest.raises(ValueError, match="at least one"):
        AudioRouter([])


@pytest.mark.asyncio
async def test_router_healthy_when_any_backend_healthy() -> None:
    a = FakeAudioBackend("a", healthy=False)
    b = FakeAudioBackend("b", healthy=True)
    router = AudioRouter([a, b])
    assert await router.healthy() is True

    a2 = FakeAudioBackend("a", healthy=False)
    assert await AudioRouter([a2]).healthy() is False


# --------------------------------------------------------------------------- #
# Circuit breaker (pure logic, injectable clock)
# --------------------------------------------------------------------------- #


def test_breaker_trips_open_after_threshold() -> None:
    t = {"now": 0.0}
    h = BackendHealth(name="x", failure_threshold=2, cooldown_s=10.0, _clock=lambda: t["now"])
    assert h.available()
    h.record_failure()
    assert h.status is BackendStatus.CLOSED
    h.record_failure()
    assert h.status is BackendStatus.OPEN
    assert not h.available()  # still in cooldown


def test_breaker_half_open_then_close() -> None:
    t = {"now": 0.0}
    h = BackendHealth(name="x", failure_threshold=1, cooldown_s=10.0, _clock=lambda: t["now"])
    h.record_failure()
    assert h.status is BackendStatus.OPEN
    t["now"] = 11.0
    assert h.available()  # cooldown elapsed -> half-open probe allowed
    assert h.status is BackendStatus.HALF_OPEN
    h.record_success()
    assert h.status is BackendStatus.CLOSED


def test_breaker_half_open_failure_reopens() -> None:
    t = {"now": 0.0}
    h = BackendHealth(name="x", failure_threshold=1, cooldown_s=5.0, _clock=lambda: t["now"])
    h.record_failure()
    t["now"] = 6.0
    h.available()  # -> half-open
    h.record_failure()
    assert h.status is BackendStatus.OPEN


@pytest.mark.asyncio
async def test_router_skips_open_backend() -> None:
    t = {"now": 0.0}
    a = FakeAudioBackend("a", script=[TransientProviderError("x"), TransientProviderError("y")])
    b = FakeAudioBackend("b")
    router = AudioRouter(
        [a, b],
        policy=AudioRouterPolicy(failure_threshold=1, cooldown_s=100.0),
        clock=lambda: t["now"],
    )
    await router.synthesize(TtsRequest(text="hi"))  # a fails -> opens, b serves
    assert router.health("audio:a").status is BackendStatus.OPEN
    a_calls_before = a.calls
    await router.synthesize(TtsRequest(text="hi"))  # a skipped (open), b serves
    assert a.calls == a_calls_before  # 'a' not retried while open


# --------------------------------------------------------------------------- #
# Async-job seam (one-shot backends via the mixin)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_oneshot_async_job_roundtrip() -> None:
    backend = FakeAudioBackend("a")
    job = await backend.submit(TtsRequest(text="hi"))
    assert isinstance(job, AudioJob)
    assert job.status is JobStatus.SUCCEEDED
    polled = await backend.poll(job)
    assert polled.status is JobStatus.SUCCEEDED
    result = await backend.fetch(job)
    assert result.provider == "a"


@pytest.mark.asyncio
async def test_router_async_job_via_lead_backend() -> None:
    a = FakeAudioBackend("a")
    router = AudioRouter([a])
    job = await router.submit(TtsRequest(text="hi"))
    assert job.status is JobStatus.SUCCEEDED
    result = await router.fetch(job)
    assert result.provider == "a"


@pytest.mark.asyncio
async def test_router_fetch_rejects_non_succeeded_job() -> None:
    router = AudioRouter([FakeAudioBackend("a")])
    job = AudioJob(job_id="j", provider="a", status=JobStatus.RUNNING)
    with pytest.raises(ValueError, match="cannot fetch"):
        await router.fetch(job)


def test_job_status_terminal() -> None:
    assert JobStatus.SUCCEEDED.is_terminal
    assert JobStatus.FAILED.is_terminal
    assert not JobStatus.PENDING.is_terminal
    assert not JobStatus.RUNNING.is_terminal
