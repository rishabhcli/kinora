"""Intent handling — debounce, dwell, idle-pause, and seek (kinora.md §4.7/§4.8).

The Scheduler reacts to *settled* reading intent, never raw scroll noise. This
controller wraps :class:`SchedulerService` with the three §4.7 timers and the
§4.8 seek handler:

* **Scroll-settle debounce (200ms).** Intents arriving inside the debounce window
  only update the latest position; the heavy control tick runs once scrolling
  pauses.
* **Dwell confirmation.** A beat is promoted only after ``w`` has moved toward it
  for two consecutive settle windows — a momentary overshoot resets the counter,
  so a flick-and-return never renders down the wrong path.
* **Idle-pause (8s).** No activity for 8 seconds halts speculation and freezes
  the committed buffer (handled inside :meth:`SchedulerService.on_event`).
* **Seek.** Cancel in-flight speculation now far from the new position, re-seed
  the focus playhead, reset velocity to default until two fresh samples, ensure
  the new position's bridge keyframe, and re-run the watermark fill.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import SessionMode
from app.scheduler.model import (
    SchedulerSession,
    SchedulerStore,
    new_trajectory_token,
)
from app.scheduler.service import IDLE_PAUSE_MS, SchedulerService, SchedulerTick
from app.scheduler.zones import DEFAULT_VELOCITY_WPS, clamp_velocity, eta_seconds

logger = get_logger("app.scheduler.intent")

#: Scroll-settle debounce window (§4.7).
DEBOUNCE_MS = 200
#: Consecutive settle windows of forward motion required before promotion (§4.7).
DWELL_WINDOWS = 2
#: Fresh velocity samples required after a seek before trusting the estimate (§4.8).
SEEK_FRESH_SAMPLES = 2


class SessionNotFoundError(LookupError):
    """Raised when an intent targets a session that was never started."""


@dataclass(slots=True)
class IntentResult:
    """The outcome of :meth:`IntentController.handle_intent_update`."""

    session: SchedulerSession
    settled: bool
    allow_promotion: bool = False
    tick: SchedulerTick | None = None


@dataclass(slots=True)
class SeekResult:
    """The outcome of :meth:`IntentController.handle_seek`."""

    session: SchedulerSession
    cancelled: int
    bridge_beat: str | None
    old_token: str
    tick: SchedulerTick | None = None


class IntentController:
    """Debounce/dwell/idle/seek front-end over the §4.9 control loop."""

    def __init__(
        self,
        *,
        service: SchedulerService,
        store: SchedulerStore,
        settings: Settings | None = None,
        debounce_ms: int = DEBOUNCE_MS,
        dwell_windows: int = DWELL_WINDOWS,
        idle_pause_ms: int = IDLE_PAUSE_MS,
        seek_cancel_threshold_s: float = 120.0,
        seek_keep_threshold_s: float = 120.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._service = service
        self._store = store
        self._settings = settings or get_settings()
        self._debounce_ms = debounce_ms
        self._dwell_windows = dwell_windows
        self._idle_pause_ms = idle_pause_ms
        self._seek_cancel_threshold_s = seek_cancel_threshold_s
        self._seek_keep_threshold_s = seek_keep_threshold_s
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _now(self, now_ms: int | None) -> int:
        return self._clock_ms() if now_ms is None else now_ms

    # -- session lifecycle --------------------------------------------------- #

    async def ensure_session(
        self,
        session_id: str,
        book_id: str,
        *,
        focus_word: int = 0,
        velocity_wps: float = DEFAULT_VELOCITY_WPS,
        mode: SessionMode = SessionMode.VIEWER,
    ) -> SchedulerSession:
        """Load an existing session or create+persist a fresh one."""
        existing = await self._store.load(session_id)
        if existing is not None:
            return existing
        session = SchedulerSession(
            session_id=session_id,
            book_id=book_id,
            focus_word=focus_word,
            velocity_wps=velocity_wps,
            mode=mode,
        )
        await self._store.save(session)
        return session

    # -- intent updates ------------------------------------------------------ #

    async def handle_intent_update(
        self,
        session_id: str,
        focus_word: int,
        velocity: float,
        mode: SessionMode | str | None = None,
        *,
        book_id: str | None = None,
        now_ms: int | None = None,
    ) -> IntentResult:
        """Apply a (debounced) intent update and run the control loop if settled."""
        now = self._now(now_ms)
        session = await self._load_or_create(session_id, book_id)
        session.last_activity_ms = now

        settled = (
            session.last_intent_ms is None
            or (now - session.last_intent_ms) >= self._debounce_ms
        )
        if not settled:
            # Mid-flick: keep the latest position but defer the heavy work (§4.7).
            session.focus_word = focus_word
            session.pending_intent = True
            await self._store.save(session)
            logger.info("intent.debounced", session_id=session_id, focus_word=focus_word)
            return IntentResult(session=session, settled=False)

        self._update_trajectory(session, focus_word, velocity, mode)
        session.last_intent_ms = now
        session.pending_intent = False

        allow_promotion = (
            session.consecutive_forward >= self._dwell_windows and not session.oscillating
        )
        tick = await self._service.on_event(
            session, allow_promotion=allow_promotion, now_ms=now
        )
        return IntentResult(
            session=session, settled=True, allow_promotion=allow_promotion, tick=tick
        )

    def _update_trajectory(
        self,
        session: SchedulerSession,
        focus_word: int,
        velocity: float,
        mode: SessionMode | str | None,
    ) -> None:
        """Update direction, dwell counter, oscillation flag, and velocity."""
        delta = focus_word - session.focus_word
        new_direction = 1 if delta > 0 else (-1 if delta < 0 else session.direction)

        if delta != 0 and new_direction != session.direction:
            # Direction flip = overshoot/oscillation; reset dwell (§4.7).
            session.oscillating = True
            session.consecutive_forward = 0
        elif delta > 0:
            session.consecutive_forward += 1
            session.oscillating = False
        session.direction = new_direction
        session.focus_word = focus_word

        # Post-seek velocity reset: default until two fresh samples arrive (§4.8).
        if session.fresh_samples_needed > 0:
            session.fresh_samples_needed -= 1
            session.velocity_wps = DEFAULT_VELOCITY_WPS
            session.raw_velocity_wps = DEFAULT_VELOCITY_WPS
        else:
            session.velocity_wps = clamp_velocity(velocity)
            session.raw_velocity_wps = abs(velocity)

        if mode is not None:
            session.mode = mode if isinstance(mode, SessionMode) else SessionMode(mode)

    # -- idle sweep ---------------------------------------------------------- #

    async def sweep_idle(
        self, session_id: str, *, now_ms: int | None = None
    ) -> SchedulerTick | None:
        """Run a tick that idle-pauses if the reader has been quiet ≥ 8s (§4.7)."""
        now = self._now(now_ms)
        session = await self._store.load(session_id)
        if session is None:
            return None
        return await self._service.on_event(session, allow_promotion=False, now_ms=now)

    # -- seek ---------------------------------------------------------------- #

    async def handle_seek(
        self, session_id: str, word: int, *, now_ms: int | None = None
    ) -> SeekResult:
        """Cancel distant speculation, re-seed at ``word``, bridge + refill (§4.8)."""
        now = self._now(now_ms)
        session = await self._store.load(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        # 1. Cancel in-flight speculation now far from the new position (old token).
        old_token = session.trajectory_token
        cancelled = await self._service.cancel_distant(
            session, new_word=word, threshold_s=self._seek_cancel_threshold_s
        )

        # 2. Re-seed the trajectory.
        session.focus_word = word
        session.trajectory_token = new_trajectory_token()
        session.velocity_wps = DEFAULT_VELOCITY_WPS
        session.raw_velocity_wps = DEFAULT_VELOCITY_WPS
        session.fresh_samples_needed = SEEK_FRESH_SAMPLES
        session.consecutive_forward = 0
        session.oscillating = False
        session.direction = 1
        session.bursting = False
        session.last_activity_ms = now
        session.last_intent_ms = now
        # Keep cached committed shots near the new position; drop the now-useless rest.
        session.committed_buffer = [
            shot
            for shot in session.committed_buffer
            if abs(eta_seconds(shot.word_index_start, word, DEFAULT_VELOCITY_WPS))
            <= self._seek_keep_threshold_s
        ]
        session.speculative_beats = []

        # 3. Instant bridge keyframe at the new position, then re-run the fill.
        bridge_beat = await self._service.ensure_bridge_keyframe(session, word)
        tick = await self._service.on_event(session, allow_promotion=True, now_ms=now)

        logger.info(
            "intent.seek",
            session_id=session_id,
            word=word,
            cancelled=cancelled,
            bridge_beat=bridge_beat,
        )
        return SeekResult(
            session=session,
            cancelled=cancelled,
            bridge_beat=bridge_beat,
            old_token=old_token,
            tick=tick,
        )

    # -- helpers ------------------------------------------------------------- #

    async def _load_or_create(
        self, session_id: str, book_id: str | None
    ) -> SchedulerSession:
        session = await self._store.load(session_id)
        if session is not None:
            return session
        if book_id is None:
            raise SessionNotFoundError(session_id)
        return await self.ensure_session(session_id, book_id)


__all__ = [
    "DEBOUNCE_MS",
    "DWELL_WINDOWS",
    "SEEK_FRESH_SAMPLES",
    "IntentController",
    "IntentResult",
    "SeekResult",
    "SessionNotFoundError",
]
