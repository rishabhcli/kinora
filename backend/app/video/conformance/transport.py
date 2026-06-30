"""A deterministic, scriptable fake transport for conformance runs.

The conformance harness must never touch the network and must never spend video
seconds. So it drives adapters against a :class:`ScriptedTransport`: an in-memory
stand-in for a hosted async video API that an adapter can submit tasks to, poll,
and download from — with **every fault injectable and deterministic**. There is
no wall clock and no RNG; advancing a task's lifecycle is an explicit ``tick``.

The transport is scripted to a :class:`~app.video.conformance.protocol.VideoCapabilities`
profile so the harness can assert capability *honesty*: an adapter that claims a
mode/duration/resolution it cannot actually drive will mis-handle the scripted
responses the transport produces for that claim, and the harness catches it.

This is *not* a real provider — it is the test double an adapter's own fake (in
:mod:`.fakes`) is built on, and it is also what the reference passing fake and
the deliberately-broken fakes share, so a single transport models the full
submit→poll→fetch→cancel lifecycle plus the §error-taxonomy fault surface.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum, auto

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)

#: A 1x1 PNG (deterministic real bytes). Adapters that "extract a last frame"
#: hand back a slice of clip bytes or this — either way, real bytes, not None.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f9d0000000049454e44ae426082"
)
#: A tiny deterministic "clip" payload (real bytes; not a valid mp4 but opaque).
CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"KINORA-FAKE-CLIP" * 4


class Fault(StrEnum):
    """A fault the transport can be scripted to raise, by canonical taxonomy.

    Each maps to exactly one shared provider-error class so the harness can
    assert the adapter translates the *wire* fault into the *right* taxonomy
    member (a 429 must surface as :class:`RateLimited`, never a bare
    :class:`ProviderError`).
    """

    NONE = auto()
    BAD_REQUEST = auto()  # HTTP 4xx → ProviderBadRequest (non-retryable)
    AUTH = auto()  # HTTP 401/403 → AuthenticationError (non-retryable)
    RATE_LIMITED = auto()  # HTTP 429 → RateLimited (retryable)
    TRANSIENT = auto()  # HTTP 5xx / blip → TransientProviderError (retryable)
    TIMEOUT = auto()  # the call never returns in time → ProviderTimeout


_FAULT_TO_ERROR: dict[Fault, type[ProviderError]] = {
    Fault.BAD_REQUEST: ProviderBadRequest,
    Fault.AUTH: AuthenticationError,
    Fault.RATE_LIMITED: RateLimited,
    Fault.TRANSIENT: TransientProviderError,
    Fault.TIMEOUT: ProviderTimeout,
}


def error_for_fault(fault: Fault, *, message: str = "scripted fault") -> ProviderError:
    """Construct the canonical provider error a wire ``fault`` must surface as."""
    if fault is Fault.NONE:
        raise ValueError("Fault.NONE has no error")
    cls = _FAULT_TO_ERROR[fault]
    if cls is RateLimited:
        return RateLimited(message, status_code=429, retry_after_s=1.0)
    status = {
        ProviderBadRequest: 400,
        AuthenticationError: 401,
        ProviderTimeout: None,
        TransientProviderError: 503,
    }.get(cls)
    return cls(message, status_code=status)


@dataclass
class _Task:
    """Internal lifecycle state of one submitted task."""

    task_id: str
    model: str
    shot_id: str | None
    #: Number of ``tick``s remaining before the task reaches a terminal state.
    ticks_to_done: int
    #: The terminal state the task lands in once ``ticks_to_done`` hits zero.
    terminal: str = "succeeded"
    state: str = "pending"
    canceled: bool = False

    def tick(self) -> None:
        if self.canceled or self.state in ("succeeded", "failed", "canceled"):
            return
        if self.ticks_to_done <= 0:
            self.state = self.terminal
            return
        self.ticks_to_done -= 1
        self.state = "running" if self.ticks_to_done > 0 else self.terminal


@dataclass
class TransportScript:
    """A deterministic plan for how the transport responds across a run.

    Attributes:
        submit_faults: Faults to raise on successive ``submit`` calls (consumed
            left-to-right; once exhausted, submit succeeds).
        poll_faults: Faults to raise on successive ``poll`` calls.
        fetch_faults: Faults to raise on successive ``fetch`` (download) calls.
        ticks_to_done: How many ``poll``s a submitted task takes to finish (0 =
            done on first poll). ``None`` means it never finishes (drives the
            timeout check).
        terminal_state: The state a finished task lands in (``succeeded`` /
            ``failed``).
        clip_bytes / last_frame_bytes: The asset bytes ``fetch`` returns.
    """

    submit_faults: deque[Fault] = field(default_factory=deque)
    poll_faults: deque[Fault] = field(default_factory=deque)
    fetch_faults: deque[Fault] = field(default_factory=deque)
    ticks_to_done: int | None = 0
    terminal_state: str = "succeeded"
    clip_bytes: bytes = CLIP_BYTES
    last_frame_bytes: bytes = PNG_1X1

    @classmethod
    def healthy(cls) -> TransportScript:
        """A no-fault script that succeeds immediately (the happy path)."""
        return cls()

    @classmethod
    def with_submit_faults(cls, faults: Iterable[Fault]) -> TransportScript:
        return cls(submit_faults=deque(faults))

    @classmethod
    def with_poll_faults(cls, faults: Iterable[Fault]) -> TransportScript:
        return cls(poll_faults=deque(faults))

    @classmethod
    def never_completes(cls) -> TransportScript:
        return cls(ticks_to_done=None)


class ScriptedTransport:
    """A deterministic in-memory async video API (submit → poll → fetch).

    No network, no clock, no RNG. The harness scripts faults and timing up front;
    the transport then behaves exactly as scripted, so every conformance check is
    fully reproducible. A real adapter's fake (see :mod:`.fakes`) wraps one of
    these and translates between :class:`WanSpec`/:class:`VideoResult` and the
    transport's primitive responses — which is precisely the request-mapping and
    error-translation behaviour the harness verifies.
    """

    def __init__(self, script: TransportScript | None = None) -> None:
        self.script = script or TransportScript.healthy()
        self._tasks: dict[str, _Task] = {}
        self._counter = 0
        #: Observable call counts — the harness asserts on these to prove
        #: idempotency (no duplicate submit) and cancellation (a cancel issued).
        self.submit_calls = 0
        self.poll_calls = 0
        self.fetch_calls = 0
        self.cancel_calls = 0
        #: shot_id → task_id, so idempotent re-submit returns the same task.
        self._by_shot: dict[str, str] = {}
        #: The last submit body the adapter sent — request-mapping assertions
        #: read this to confirm canonical fields round-tripped.
        self.last_submit_body: dict[str, object] | None = None

    # -- the transport API an adapter calls ------------------------------- #

    def submit(
        self,
        body: dict[str, object],
        *,
        shot_id: str | None = None,
    ) -> str:
        """Submit a task; return its id. Raises a scripted fault if present.

        Idempotent on ``shot_id``: a re-submit with a seen ``shot_id`` returns
        the same task id and does **not** create a new task (so the harness can
        prove an adapter that claims idempotency never double-spends).
        """
        self.submit_calls += 1
        self.last_submit_body = dict(body)
        self._maybe_raise(self.script.submit_faults)
        if shot_id is not None and shot_id in self._by_shot:
            return self._by_shot[shot_id]
        self._counter += 1
        task_id = f"task-{self._counter:04d}"
        ticks = self.script.ticks_to_done
        task = _Task(
            task_id=task_id,
            model=str(body.get("model", "?")),
            shot_id=shot_id,
            # None ticks_to_done → effectively never completes (huge tick budget).
            ticks_to_done=(1 << 30) if ticks is None else ticks,
            terminal=self.script.terminal_state,
        )
        self._tasks[task_id] = task
        if shot_id is not None:
            self._by_shot[shot_id] = task_id
        return task_id

    def poll(self, task_id: str) -> str:
        """Advance and return a task's state. Raises a scripted poll fault first."""
        self.poll_calls += 1
        self._maybe_raise(self.script.poll_faults)
        task = self._require(task_id)
        task.tick()
        return task.state

    def clip_url(self, task_id: str) -> str:
        """The (expiring) result URL for a succeeded task."""
        task = self._require(task_id)
        if task.state != "succeeded":
            raise ProviderError(f"task {task_id} is {task.state}, no clip url")
        # Deliberately "expiring": the adapter must download eagerly, not stash this.
        return f"https://fake.invalid/clips/{task_id}.mp4?expires=1"

    def download(self, _url: str) -> bytes:
        """Download clip bytes. Raises a scripted fetch fault first."""
        self.fetch_calls += 1
        self._maybe_raise(self.script.fetch_faults)
        return self.script.clip_bytes

    def last_frame(self, _task_id: str) -> bytes:
        """The clip's last-frame bytes (for continuation chaining)."""
        return self.script.last_frame_bytes

    def cancel(self, task_id: str) -> None:
        """Cancel an in-flight task; subsequent polls report ``canceled``."""
        self.cancel_calls += 1
        task = self._require(task_id)
        task.canceled = True
        task.state = "canceled"

    # -- internals -------------------------------------------------------- #

    def _require(self, task_id: str) -> _Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise ProviderBadRequest(f"unknown task {task_id}")
        return task

    @staticmethod
    def _maybe_raise(faults: deque[Fault]) -> None:
        if not faults:
            return
        fault = faults.popleft()
        if fault is not Fault.NONE:
            raise error_for_fault(fault)


__all__ = [
    "CLIP_BYTES",
    "PNG_1X1",
    "Fault",
    "ScriptedTransport",
    "TransportScript",
    "error_for_fault",
]
