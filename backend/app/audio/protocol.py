"""The :class:`UniversalAudioProvider` Protocol — one audio backend contract.

The audio analogue of :class:`app.providers.video_router.VideoBackend`: a single
structural contract every audio source satisfies, so narration / music / SFX can
come from any model (DashScope CosyVoice/Qwen3-TTS, ElevenLabs, OpenAI TTS,
Azure/Google, a generic descriptor-driven adapter) behind one seam — and a router
can fail over between them with no caller change.

Two surfaces, because audio models split into two shapes:

* **One-shot** (``capabilities`` + ``synthesize`` + ``healthy``) — the fast path
  the Generator uses; returns the finished :class:`AudioResult` directly.
* **Async job** (``submit`` + ``poll`` + ``fetch``) — for slow models that run as
  a submit→poll→fetch task (some music-gen / long-form engines). The default
  :class:`OneShotAudioProvider` mixin synthesizes eagerly so a one-shot backend is
  *also* a valid async-job backend with zero extra code; genuinely-async adapters
  override the three job methods.

A duration helper (:func:`wav_duration`) lives here because every adapter needs to
measure a returned WAV the same robust way (streaming WAV headers lie about frame
counts), and the alignment fallback is anchored to that real measured length.
"""

from __future__ import annotations

import contextlib
import io
import wave
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .types import AudioCapability, AudioResult, TtsRequest


class JobStatus(StrEnum):
    """The lifecycle state of an async audio synthesis job."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True once the job will not change state again."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class AudioJob(BaseModel):
    """A handle to an in-flight async audio synthesis job.

    ``submit`` returns one; ``poll`` returns an updated copy; ``fetch`` turns a
    SUCCEEDED job into an :class:`AudioResult`. Provider-neutral so the router can
    drive any async backend identically.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    provider: str
    status: JobStatus = JobStatus.PENDING
    #: Carried from the originating request for fetch-time alignment + telemetry.
    request: TtsRequest | None = None
    #: Human-readable failure detail when ``status`` is FAILED.
    error: str | None = None


@runtime_checkable
class UniversalAudioProvider(Protocol):
    """One source of synthesized audio the router can route to.

    The drop-in seam for ``generator.py``'s tts call: any object exposing these
    members is interchangeable. ``synthesize`` is the one-shot fast path; the
    ``submit`` / ``poll`` / ``fetch`` trio is the async-job path for slow models
    (a one-shot backend satisfies both via :class:`OneShotAudioProvider`).
    """

    #: Stable identity for routing, health bookkeeping, and telemetry.
    name: str

    def capabilities(self) -> AudioCapability:
        """The backend's capability profile (voices, timing, limits, …)."""
        ...

    async def synthesize(self, request: TtsRequest) -> AudioResult:
        """Synthesize ``request`` to a finished :class:`AudioResult` (one-shot)."""
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe (no synthesis spend); ``True`` when routable."""
        ...

    async def submit(self, request: TtsRequest) -> AudioJob:
        """Submit an async synthesis job; returns its :class:`AudioJob` handle."""
        ...

    async def poll(self, job: AudioJob) -> AudioJob:
        """Return an updated :class:`AudioJob` with the current job status."""
        ...

    async def fetch(self, job: AudioJob) -> AudioResult:
        """Fetch the finished audio for a SUCCEEDED ``job``."""
        ...


class OneShotAudioProvider:
    """Mixin giving a one-shot backend the async-job surface for free.

    A backend that implements ``capabilities`` + ``synthesize`` + ``healthy`` and
    mixes this in is *also* a valid async-job :class:`UniversalAudioProvider`:
    ``submit`` synthesizes eagerly and stashes the result, ``poll`` reports it
    SUCCEEDED, and ``fetch`` returns it. Genuinely-async adapters skip the mixin
    and implement the trio against their real task API.
    """

    name: str

    def capabilities(self) -> AudioCapability:  # pragma: no cover - provided by subclass
        raise NotImplementedError

    async def synthesize(self, request: TtsRequest) -> AudioResult:  # pragma: no cover
        raise NotImplementedError

    async def healthy(self) -> bool:
        """Default liveness probe: backends are routable unless they override."""
        return True

    async def submit(self, request: TtsRequest) -> AudioJob:
        """Synthesize eagerly and return a SUCCEEDED job carrying the result."""
        result = await self.synthesize(request)
        job = AudioJob(
            job_id=result.provider_job_id or f"{self.name}:oneshot",
            provider=result.provider,
            status=JobStatus.SUCCEEDED,
            request=request,
        )
        self._eager_results[job.job_id] = result
        return job

    async def poll(self, job: AudioJob) -> AudioJob:
        """A one-shot job is already terminal; echo it back unchanged."""
        return job

    async def fetch(self, job: AudioJob) -> AudioResult:
        """Return the eagerly-synthesized result for ``job``."""
        try:
            return self._eager_results[job.job_id]
        except KeyError as exc:  # pragma: no cover - misuse guard
            raise KeyError(f"no eager result for job {job.job_id!r}") from exc

    @property
    def _eager_results(self) -> dict[str, AudioResult]:
        cache = getattr(self, "_oneshot_cache", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_oneshot_cache", cache)
        return cache


def wav_duration(raw: bytes, default_sr: int) -> tuple[float, int]:
    """Return ``(duration_s, sample_rate)`` for WAV ``raw``, robust to bad headers.

    Streaming TTS WAVs ship a placeholder frame count in the header, so cross-check
    the declared frames against the actual byte length and trust whichever is
    consistent. Non-WAV / unparsable bytes fall back to a 16-bit-mono estimate at
    ``default_sr`` (the alignment fallback only needs an anchor length, not codec
    truth). Mirrors :func:`app.providers.tts._wav_duration`.
    """
    try:
        with contextlib.closing(wave.open(io.BytesIO(raw))) as wf:
            sr = wf.getframerate() or default_sr
            frame_bytes = max(wf.getsampwidth() * wf.getnchannels(), 1)
            frames_from_bytes = max(len(raw) - 44, 0) / frame_bytes
            declared = wf.getnframes()
            frames = declared if 0 < declared <= frames_from_bytes * 4 else frames_from_bytes
            return max(frames / sr, 0.0), sr
    except (wave.Error, EOFError, OSError):
        return max((len(raw) - 44) / 2 / default_sr, 0.0), default_sr


__all__ = [
    "AudioJob",
    "JobStatus",
    "OneShotAudioProvider",
    "UniversalAudioProvider",
    "wav_duration",
]
