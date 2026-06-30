"""Multi-provider audio routing — capability-aware, health-based failover.

The audio analogue of :class:`app.providers.video_router.VideoRouter`: a thin,
deterministic router in front of one or more :class:`UniversalAudioProvider` s so
the Generator keeps calling a single ``synthesize(TtsRequest)`` while gaining
provider failover underneath. Narration can come from DashScope today and silently
fail over to ElevenLabs/OpenAI/Azure tomorrow with no caller change.

Design rules (mirroring the video router so the two behave identically):

* **Capability-aware selection.** Before routing, the router skips backends whose
  :class:`~app.audio.types.AudioCapability` can't serve the request (wrong track
  type, unsupported voice/language, SSML when unsupported, over the char limit).
  Only *capable, healthy* backends are tried.
* **Health is pure logic.** :class:`BackendHealth` is a small circuit breaker driven
  by an injectable monotonic clock — no wall-clock, no RNG, exhaustively testable.
* **Non-retryable errors short-circuit.** A capability/value error means the request
  is wrong for that backend; the router advances. A retryable transport fault
  advances to the next backend; after all are exhausted the last error is raised.

The router itself satisfies :class:`UniversalAudioProvider`, so it can nest and the
Generator cannot tell how many real backends sit underneath.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.providers.errors import ProviderError

from .protocol import AudioJob, JobStatus, UniversalAudioProvider
from .types import AudioCapability, AudioResult, TtsRequest

logger = get_logger("app.audio.router")

#: A monotonic clock source (seconds). Injectable so tests advance time exactly.
Clock = Callable[[], float]


class BackendStatus(StrEnum):
    """The circuit state of one backend in the router."""

    CLOSED = "closed"  # healthy; route freely
    OPEN = "open"  # tripped; skip until the cooldown elapses
    HALF_OPEN = "half_open"  # cooldown elapsed; allow one probe attempt


@dataclass
class BackendHealth:
    """A small per-backend circuit breaker driving router ordering (pure logic).

    Trips ``OPEN`` after ``failure_threshold`` consecutive failures; after
    ``cooldown_s`` allows a single ``HALF_OPEN`` probe whose outcome closes it
    (success) or re-opens it (failure). Identical in shape to the video router's.
    """

    name: str
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    _clock: Clock = field(default=time.monotonic, repr=False)
    status: BackendStatus = BackendStatus.CLOSED
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    opened_at: float = 0.0

    def available(self) -> bool:
        """True when the breaker would let a call through right now."""
        if self.status is BackendStatus.OPEN:
            if self._clock() - self.opened_at >= self.cooldown_s:
                self.status = BackendStatus.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self.total_successes += 1
        self.consecutive_failures = 0
        self.status = BackendStatus.CLOSED

    def record_failure(self) -> None:
        self.total_failures += 1
        self.consecutive_failures += 1
        if self.status is BackendStatus.HALF_OPEN or (
            self.consecutive_failures >= self.failure_threshold
        ):
            self.status = BackendStatus.OPEN
            self.opened_at = self._clock()


@dataclass(frozen=True, slots=True)
class AudioRouterPolicy:
    """Tunables for :class:`AudioRouter` (deterministic; no env reads)."""

    failure_threshold: int = 3
    cooldown_s: float = 30.0
    #: When True, the router prefers a backend that emits *inline* word timestamps
    #: (``supports_word_timestamps``) for narration requests that ask for timing,
    #: so karaoke gets the most precise map available; ties keep priority order.
    prefer_inline_timestamps: bool = False


def can_serve(capability: AudioCapability, request: TtsRequest) -> bool:
    """True when a backend with ``capability`` can serve ``request`` (pure).

    The same gate :class:`~app.audio.adapters.descriptor.DescriptorAudioProvider`
    enforces, lifted out so the router can pre-filter candidates *before* spending
    a synth call on a backend that would reject the request.
    """
    if not capability.supports_track(request.track_type):
        return False
    if not capability.supports_voice(request.voice_id):
        return False
    if not capability.supports_language(request.language):
        return False
    if request.is_ssml and not capability.supports_ssml:
        return False
    if request.voice_clone_reference is not None and not capability.supports_voice_cloning:
        return False
    return not (
        capability.max_input_chars is not None
        and request.char_count() > capability.max_input_chars
    )


class AudioRouter:
    """Route a :class:`TtsRequest` across ordered :class:`UniversalAudioProvider` s.

    Construction takes backends in **priority order** (first = preferred). The
    router implements :class:`UniversalAudioProvider`, so it is a drop-in for the
    Generator's tts seam and can nest inside another router.
    """

    def __init__(
        self,
        backends: Sequence[UniversalAudioProvider],
        *,
        policy: AudioRouterPolicy | None = None,
        clock: Clock = time.monotonic,
        name: str = "audio-router",
    ) -> None:
        if not backends:
            raise ValueError("AudioRouter requires at least one backend")
        self.name = name
        self._policy = policy or AudioRouterPolicy()
        self._backends: list[UniversalAudioProvider] = list(backends)
        self._health: dict[str, BackendHealth] = {
            b.name: BackendHealth(
                name=b.name,
                failure_threshold=self._policy.failure_threshold,
                cooldown_s=self._policy.cooldown_s,
                _clock=clock,
            )
            for b in self._backends
        }

    # -- introspection ---------------------------------------------------- #

    def capabilities(self) -> AudioCapability:
        """The preferred (first) backend's capability profile.

        The router presents its lead backend's profile as its own face; callers
        that need per-backend detail iterate :meth:`backends`.
        """
        return self._backends[0].capabilities()

    def backends(self) -> list[UniversalAudioProvider]:
        """All backends in priority order."""
        return list(self._backends)

    def health(self, name: str) -> BackendHealth:
        """The :class:`BackendHealth` record for backend ``name``."""
        return self._health[name]

    def available_backends(self) -> list[UniversalAudioProvider]:
        """Backends whose breaker currently permits a call, in priority order."""
        return [b for b in self._backends if self._health[b.name].available()]

    async def healthy(self) -> bool:
        """True when *any* available backend reports healthy."""
        for backend in self.available_backends():
            try:
                if await backend.healthy():
                    return True
            except ProviderError:
                continue
        return False

    # -- candidate ordering ----------------------------------------------- #

    def _candidates(self, request: TtsRequest) -> list[UniversalAudioProvider]:
        """Capable, healthy backends ordered for this request (pure-ish)."""
        capable = [
            b
            for b in self.available_backends()
            if can_serve(b.capabilities(), request)
        ]
        if (
            self._policy.prefer_inline_timestamps
            and request.word_timestamps
            and request.track_type.value == "narration"
        ):
            # Stable sort: inline-timestamp backends first, else priority order.
            capable.sort(
                key=lambda b: (0 if b.capabilities().supports_word_timestamps else 1)
            )
        return capable

    # -- synthesize (one-shot) -------------------------------------------- #

    async def synthesize(self, request: TtsRequest) -> AudioResult:
        """Synthesize via the first capable, healthy backend; fail over on faults.

        Raises:
            ValueError: when no backend can serve the request (capability gap).
            ProviderError: the last retryable transport error after every capable
                backend has been tried.
        """
        candidates = self._candidates(request)
        if not candidates:
            raise ValueError(
                "no audio backend can serve the request "
                f"(track={request.track_type.value}, voice={request.voice_id!r})"
            )
        last_error: ProviderError | None = None
        for backend in candidates:
            try:
                result = await backend.synthesize(request)
            except ProviderError as exc:
                self._health[backend.name].record_failure()
                last_error = exc
                logger.warning(
                    "audio_router.backend_failed",
                    backend=backend.name,
                    retryable=exc.retryable,
                    error=type(exc).__name__,
                )
                if not exc.retryable:
                    raise
                continue
            except ValueError:
                # A capability mismatch the pre-filter missed; not a health fault.
                logger.warning("audio_router.backend_rejected", backend=backend.name)
                continue
            self._health[backend.name].record_success()
            logger.info("audio_router.routed", backend=backend.name)
            return result
        if last_error is not None:
            raise last_error
        raise ValueError("every audio backend rejected the request")

    # -- async-job surface (delegated to the chosen backend) -------------- #

    async def submit(self, request: TtsRequest) -> AudioJob:
        """Submit an async job to the first capable, healthy backend."""
        candidates = self._candidates(request)
        if not candidates:
            raise ValueError("no audio backend can serve the request")
        return await candidates[0].submit(request)

    async def poll(self, job: AudioJob) -> AudioJob:
        """Poll a job on the backend that owns it (matched by provider name)."""
        return await self._backend_for_job(job).poll(job)

    async def fetch(self, job: AudioJob) -> AudioResult:
        """Fetch a finished job from the backend that owns it."""
        if job.status is not JobStatus.SUCCEEDED:
            raise ValueError(f"cannot fetch a job in status {job.status.value}")
        return await self._backend_for_job(job).fetch(job)

    def _backend_for_job(self, job: AudioJob) -> UniversalAudioProvider:
        for backend in self._backends:
            if backend.capabilities().provider == job.provider:
                return backend
        # Fall back to the lead backend (one-shot eager jobs carry its result).
        return self._backends[0]


__all__ = [
    "AudioRouter",
    "AudioRouterPolicy",
    "BackendHealth",
    "BackendStatus",
    "Clock",
    "can_serve",
]
