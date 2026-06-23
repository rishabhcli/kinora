"""The Scheduler control loop (kinora.md §4.5–§4.9).

:class:`SchedulerService.on_event` is the §4.9 control tick, run on every
debounced intent update or job-completion event. In order it:

1. **idle-pauses** — if the reader has gone quiet (§4.7), cancel speculative work
   and freeze the committed buffer;
2. **refreshes** committed-seconds-ahead from the buffer relative to ``w``;
3. **fills the committed buffer under dual-watermark hysteresis** (§4.5): a burst
   begins only when the buffer drains below ``L`` and runs until it reaches ``H``,
   then goes idle — promoting shots whose ETA crosses the commit horizon ``C``
   while the trajectory is stable and the budget can afford it (velocity-adaptive
   promotion, §4.6), reserving video-seconds per promotion;
4. **maintains cheap keyframes** across the speculative horizon — image stills,
   **zero video-seconds** (§4.4);
5. **enforces caps / backpressure** (§12.2).

Every collaborator is a narrow Protocol so the real Redis queue, budget service,
source-span repo, and keyframe lane fit — and tests can inject light doubles.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.base import new_id
from app.db.models.enums import RenderPriority
from app.memory.budget_service import BudgetExceeded, Reservation
from app.observability import metrics
from app.queue.redis_queue import PREEMPTIBLE_LANES, EnqueueResult
from app.scheduler.events import SessionEventPublisher
from app.scheduler.model import BufferedShot, SchedulerSession, SchedulerStore
from app.scheduler.zones import Zone, eta_seconds, trajectory_is_stable, viewer_zone

logger = get_logger("app.scheduler.service")

#: Idle-pause threshold (§4.7): no activity for this long halts speculation.
IDLE_PAUSE_MS = 8_000


# --------------------------------------------------------------------------- #
# Injected collaborator protocols
# --------------------------------------------------------------------------- #


class SchedulerShot(Protocol):
    """The slice of a ``shots`` row the Scheduler reads from the span index."""

    id: str
    beat_id: str | None
    scene_id: str | None
    source_span: dict[str, Any] | None
    duration_s: float | None


class ShotSource(Protocol):
    """The §4.2 source-span index seam (real: :class:`SourceSpanRepo`)."""

    async def next_uncommitted_shot(
        self, book_id: str, after_word: int
    ) -> SchedulerShot | None: ...

    async def resolve_word_to_shot(
        self, book_id: str, word_index: int
    ) -> SchedulerShot | None: ...


class BudgetGate(Protocol):
    """The budget seam (real: :class:`BudgetService`) — gate + reserve/release."""

    def can_render_live(self) -> bool: ...

    async def is_low(self) -> bool: ...

    async def remaining(self) -> float: ...

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation: ...

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None: ...


class RenderQueue(Protocol):
    """The render-queue seam (real: :class:`RedisRenderQueue`)."""

    async def enqueue(
        self,
        *,
        shot_hash: str,
        priority: RenderPriority,
        book_id: str,
        job_id: str,
        session_id: str | None = None,
        shot_id: str | None = None,
        beat_id: str | None = None,
        scene_id: str | None = None,
        cancel_token: str | None = None,
        reservation_id: str | None = None,
        reserved_video_s: float = 0.0,
        target_duration_s: float = 5.0,
        target_word: int = 0,
        prompt: str | None = None,
        now_ms: int | None = None,
    ) -> EnqueueResult: ...

    async def cancel_by_token(
        self, token: str, *, lanes: Sequence[RenderPriority] | None = None
    ) -> int: ...

    async def cancel_distant(
        self,
        token: str,
        *,
        focus_word: int,
        velocity_wps: float,
        threshold_s: float = 120.0,
        lanes: Sequence[RenderPriority] = PREEMPTIBLE_LANES,
    ) -> int: ...


class KeyframeMaintainer(Protocol):
    """The keyframe-lane seam — ensure a beat's still (no video-seconds, §4.4)."""

    async def ensure(
        self,
        session: SchedulerSession,
        *,
        book_id: str,
        beat_id: str,
        target_word: int,
        prompt: str | None = None,
    ) -> Any: ...


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SchedulerTick:
    """What one :meth:`SchedulerService.on_event` did (for events/metrics/tests)."""

    idle: bool = False
    promoted: list[str] = field(default_factory=list)
    keyframed: list[str] = field(default_factory=list)
    cancelled: int = 0
    committed_seconds_ahead: float = 0.0
    bursting: bool = False


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class SchedulerService:
    """The dedicated prefetch controller (distinct from the Showrunner, §4.9)."""

    def __init__(
        self,
        *,
        queue: RenderQueue,
        budget: BudgetGate,
        shots: ShotSource,
        keyframes: KeyframeMaintainer,
        store: SchedulerStore | None = None,
        settings: Settings | None = None,
        events: SessionEventPublisher | None = None,
        idle_pause_ms: int = IDLE_PAUSE_MS,
        keyframe_cap: int = 12,
    ) -> None:
        self._queue = queue
        self._budget = budget
        self._shots = shots
        self._keyframes = keyframes
        self._store = store
        self._events = events
        settings = settings or get_settings()
        self._low = settings.watermark_low_s
        self._high = settings.watermark_high_s
        self._commit_horizon = settings.commit_horizon_s
        self._spec_horizon = settings.spec_horizon_s
        self._idle_pause_ms = idle_pause_ms
        self._keyframe_cap = keyframe_cap

    @property
    def watermarks(self) -> tuple[float, float, float, float]:
        """``(L, H, C, SPEC)`` — the active watermarks/horizons (read-only)."""
        return (self._low, self._high, self._commit_horizon, self._spec_horizon)

    async def on_event(
        self,
        session: SchedulerSession,
        *,
        allow_promotion: bool = True,
        now_ms: int | None = None,
    ) -> SchedulerTick:
        """Run one control tick for ``session`` (the §4.9 loop, exactly)."""
        # 1. Idle-pause: halt speculation, freeze the committed buffer (§4.7).
        if self._is_idle(session, now_ms):
            cancelled = await self._cancel_speculative(session)
            session.bursting = False
            # An idle *period* begins when we actually halt in-flight speculation;
            # subsequent idle ticks cancel nothing and are not re-counted.
            if cancelled:
                metrics.inc_idle_period()
            metrics.set_buffer_occupancy(session.session_id, session.committed_seconds_ahead)
            await self._save(session)
            await self._publish_buffer_state(session, idle=True)
            logger.info("scheduler.idle", session_id=session.session_id, cancelled=cancelled)
            return SchedulerTick(
                idle=True,
                cancelled=cancelled,
                committed_seconds_ahead=session.committed_seconds_ahead,
            )

        # 2. Refresh committed-seconds-ahead from the buffer relative to w.
        session.recompute_committed_ahead()
        session.budget_remaining_s = await self._budget.remaining()
        await self._maybe_publish_budget_low(session)

        # 3. Dual-watermark hysteresis fill (velocity-adaptive promotion).
        was_bursting = session.bursting
        promoted: list[str] = []
        if allow_promotion:
            promoted = await self._fill_committed(session)
        if session.bursting != was_bursting:
            metrics.inc_watermark_crossing("low" if session.bursting else "high")
        metrics.inc_promotions(len(promoted))

        # 4. Cheap keyframes across the speculative horizon (zero video-seconds).
        keyframed = await self._maintain_keyframes(session)

        # 5. Caps & backpressure (§12.2).
        self._enforce_caps(session)

        await self._save(session)
        metrics.set_buffer_occupancy(session.session_id, session.committed_seconds_ahead)
        await self._publish_buffer_state(session, idle=False, promoted=len(promoted))
        tick = SchedulerTick(
            idle=False,
            promoted=promoted,
            keyframed=keyframed,
            committed_seconds_ahead=session.committed_seconds_ahead,
            bursting=session.bursting,
        )
        logger.info(
            "scheduler.tick",
            session_id=session.session_id,
            focus_word=session.focus_word,
            velocity=session.velocity_wps,
            committed_ahead=round(session.committed_seconds_ahead, 2),
            bursting=session.bursting,
            promoted=len(promoted),
            keyframed=len(keyframed),
        )
        return tick

    async def _maybe_publish_budget_low(self, session: SchedulerSession) -> None:
        """Announce ``budget_low`` once per low-budget episode (§5.6/§11.1)."""
        low = await self._budget.is_low()
        if not low:
            session.budget_low_announced = False
            return
        if session.budget_low_announced or self._events is None:
            return
        remaining = session.budget_remaining_s
        if remaining is None:
            remaining = await self._budget.remaining()
        await self._events.publish(
            session.session_id,
            {"event": "budget_low", "remaining_s": remaining},
        )
        session.budget_low_announced = True
        logger.info(
            "scheduler.budget_low",
            session_id=session.session_id,
            remaining_s=remaining,
        )

    # -- buffer surfacing (the §5.3 indicator) ------------------------------- #

    async def _zone_and_eta(self, session: SchedulerSession) -> tuple[Zone, float | None]:
        """Classify the nearest upcoming shot + its ETA for the §5.3 surfacing."""
        shot = await self._shots.next_uncommitted_shot(session.book_id, session.focus_word)
        next_eta = (
            eta_seconds(_shot_start(shot), session.focus_word, session.velocity_wps)
            if shot is not None
            else None
        )
        budget_ok = self._budget.can_render_live() and not await self._budget.is_low()
        zone = viewer_zone(
            next_eta,
            stable=trajectory_is_stable(session),
            budget_ok=budget_ok,
            commit_horizon_s=self._commit_horizon,
            spec_horizon_s=self._spec_horizon,
        )
        return zone, next_eta

    async def _publish_buffer_state(
        self, session: SchedulerSession, *, idle: bool, promoted: int = 0
    ) -> None:
        """Surface live buffer occupancy + zone to the client (§5.3/§5.6).

        A small event carrying everything the buffer hairline + zone badge + debug
        readout need: the committed-seconds-ahead the hairline fills toward ``H``,
        the watermarks/horizon it is measured against, the burst/idle flags, the
        velocity-adaptive zone + the ETA it was derived from, the in-flight render
        counts, and how many shots this tick promoted (a *real* burst). Fired once
        per control tick; a ``None`` publisher (the tests' default) makes it a
        no-op.
        """
        if self._events is None:
            return
        zone, next_eta = await self._zone_and_eta(session)
        inflight = session.inflight
        await self._events.publish(
            session.session_id,
            {
                "event": "buffer_state",
                "committed_seconds_ahead": round(session.committed_seconds_ahead, 3),
                "low": self._low,
                "high": self._high,
                "commit_horizon": self._commit_horizon,
                "bursting": session.bursting,
                "idle": idle,
                "zone": zone.value,
                "eta_next_s": round(next_eta, 3) if next_eta is not None else None,
                "velocity_wps": round(session.velocity_wps, 3),
                "inflight_committed": len(inflight["committed"]),
                "inflight_speculative": len(inflight["speculative"]),
                "promoted": promoted,
                "budget_remaining_s": session.budget_remaining_s,
            },
        )

    # -- watermark fill ------------------------------------------------------ #

    async def _fill_committed(self, session: SchedulerSession) -> list[str]:
        """Fill the committed buffer between watermarks with hysteresis (§4.5/§4.6).

        Burst on-set is gated by the **low** watermark and burst-off by the
        **high** watermark; between them the committed lane is idle. Inside a
        burst, each candidate is promoted only while its ETA is under the commit
        horizon, the trajectory is stable, and the budget can afford it.
        """
        # Hysteresis state machine.
        if session.committed_seconds_ahead < self._low:
            session.bursting = True
        if session.committed_seconds_ahead >= self._high:
            session.bursting = False
        if not session.bursting:
            return []

        can_promote = self._budget.can_render_live() and not await self._budget.is_low()
        stable = trajectory_is_stable(session)
        promoted: list[str] = []
        buffered_ids = {b.shot_id for b in session.committed_buffer}
        cursor = session.focus_word

        while session.committed_seconds_ahead < self._high:
            shot = await self._shots.next_uncommitted_shot(session.book_id, cursor)
            if shot is None:
                break
            start = _shot_start(shot)
            cursor = max(cursor + 1, start)  # guarantee forward progress
            if shot.id in buffered_ids:
                continue

            eta = eta_seconds(start, session.focus_word, session.velocity_wps)
            if eta >= self._commit_horizon or not stable:
                break  # beyond the commit horizon / skimming -> ride keyframes

            est = _shot_duration(shot)
            if not can_promote or await self._budget.remaining() < est:
                break  # cannot spend video-seconds -> ride the keyframe ladder

            reservation = await self._reserve(session, shot, est)
            if reservation is None:
                break  # a cap was hit -> stop promoting

            result = await self._queue.enqueue(
                shot_hash=_dedup_key(session.book_id, shot),
                priority=RenderPriority.COMMITTED,
                book_id=session.book_id,
                job_id=new_id(),
                session_id=session.session_id,
                shot_id=shot.id,
                beat_id=shot.beat_id,
                scene_id=shot.scene_id,
                cancel_token=session.trajectory_token,
                reservation_id=reservation.id,
                reserved_video_s=est,
                target_duration_s=est,
                target_word=start,
            )
            if not result.admitted:
                # Committed is always admitted; defensively release on a drop.
                await self._budget.release(reservation, note="committed dropped")
                break
            if not result.created:
                # Already in-flight/known (idempotent): don't double-reserve.
                await self._budget.release(reservation, note="dedup")
                if shot.id not in buffered_ids:
                    session.committed_buffer.append(
                        BufferedShot(
                            shot_id=shot.id, word_index_start=start, est_duration_s=est
                        )
                    )
                    buffered_ids.add(shot.id)
                    session.committed_seconds_ahead += est
                continue

            session.committed_buffer.append(
                BufferedShot(shot_id=shot.id, word_index_start=start, est_duration_s=est)
            )
            buffered_ids.add(shot.id)
            session.committed_seconds_ahead += est
            promoted.append(shot.id)

        if session.committed_seconds_ahead >= self._high:
            session.bursting = False
        return promoted

    async def _reserve(
        self, session: SchedulerSession, shot: SchedulerShot, est: float
    ) -> Reservation | None:
        """Reserve the gating earmark for a promotion (released by the worker)."""
        try:
            return await self._budget.reserve(
                est,
                session_id=session.session_id,
                scene_id=shot.scene_id,
                book_id=session.book_id,
                note=f"promote {shot.id}",
            )
        except BudgetExceeded as exc:
            logger.info("scheduler.budget_blocked", session_id=session.session_id, scope=exc.scope)
            return None

    # -- keyframe maintenance ------------------------------------------------ #

    async def _maintain_keyframes(self, session: SchedulerSession) -> list[str]:
        """Ensure a keyframe still for every upcoming beat not promoted (§4.4/§4.6)."""
        buffered_ids = {b.shot_id for b in session.committed_buffer}
        seen_beats = set(session.speculative_beats)
        ensured: list[str] = []
        cursor = session.focus_word
        count = 0

        while count < self._keyframe_cap:
            shot = await self._shots.next_uncommitted_shot(session.book_id, cursor)
            if shot is None:
                break
            start = _shot_start(shot)
            cursor = max(cursor + 1, start)
            eta = eta_seconds(start, session.focus_word, session.velocity_wps)
            if eta > self._spec_horizon:
                break  # cold zone -> stop (plan/canon only)
            if shot.id in buffered_ids:
                continue  # promoted to full video; no keyframe needed
            beat_id = shot.beat_id
            if not beat_id or beat_id in seen_beats:
                continue
            await self._keyframes.ensure(
                session,
                book_id=session.book_id,
                beat_id=beat_id,
                target_word=start,
                prompt=_shot_prompt(shot),
            )
            seen_beats.add(beat_id)
            session.speculative_beats.append(beat_id)
            ensured.append(beat_id)
            count += 1
        return ensured

    # -- cancellation / caps ------------------------------------------------- #

    async def _cancel_speculative(self, session: SchedulerSession) -> int:
        """Cancel in-flight speculative/keyframe work; keep committed (§4.7)."""
        return await self._queue.cancel_by_token(
            session.trajectory_token, lanes=PREEMPTIBLE_LANES
        )

    async def cancel_distant(
        self, session: SchedulerSession, *, new_word: int, threshold_s: float = 120.0
    ) -> int:
        """Cancel in-flight speculative jobs now > ``threshold_s`` away (§4.8 seek)."""
        metrics.inc_seek_event()
        velocity = session.velocity_wps or 4.0
        return await self._queue.cancel_distant(
            session.trajectory_token,
            focus_word=new_word,
            velocity_wps=velocity,
            threshold_s=threshold_s,
        )

    async def ensure_bridge_keyframe(
        self, session: SchedulerSession, word: int
    ) -> str | None:
        """Ensure the keyframe for the shot at ``word`` exists (the seek bridge, §4.8)."""
        shot = await self._shots.resolve_word_to_shot(session.book_id, word)
        if shot is None or not shot.beat_id:
            return None
        await self._keyframes.ensure(
            session,
            book_id=session.book_id,
            beat_id=shot.beat_id,
            target_word=_shot_start(shot),
            prompt=_shot_prompt(shot),
        )
        if shot.beat_id not in session.speculative_beats:
            session.speculative_beats.append(shot.beat_id)
        return shot.beat_id

    def _enforce_caps(self, session: SchedulerSession) -> None:
        """Bound the speculative keyframe set (§12.2 backpressure mirror)."""
        if len(session.speculative_beats) > self._keyframe_cap:
            session.speculative_beats = session.speculative_beats[-self._keyframe_cap :]

    # -- persistence --------------------------------------------------------- #

    async def _save(self, session: SchedulerSession) -> None:
        if self._store is not None:
            await self._store.save(session)

    def _is_idle(self, session: SchedulerSession, now_ms: int | None) -> bool:
        if session.last_activity_ms is None or now_ms is None:
            return False
        return (now_ms - session.last_activity_ms) >= self._idle_pause_ms


# --------------------------------------------------------------------------- #
# Real keyframe maintainer (enqueues KEYFRAME-lane jobs)
# --------------------------------------------------------------------------- #


class QueueKeyframeMaintainer:
    """A :class:`KeyframeMaintainer` that enqueues preemptible keyframe jobs.

    The actual image generation runs on the worker's keyframe pool
    (:class:`app.scheduler.keyframe.KeyframeService`) — **zero video-seconds**.
    Keyframe jobs are keyed per beat so they dedup across sessions (§12.3).
    """

    def __init__(self, queue: Any) -> None:
        self._queue = queue

    async def ensure(
        self,
        session: SchedulerSession,
        *,
        book_id: str,
        beat_id: str,
        target_word: int,
        prompt: str | None = None,
    ) -> EnqueueResult:
        return await self._queue.enqueue(
            shot_hash=f"keyframe:{book_id}:{beat_id}",
            priority=RenderPriority.KEYFRAME,
            book_id=book_id,
            job_id=new_id(),
            session_id=session.session_id,
            beat_id=beat_id,
            cancel_token=session.trajectory_token,
            reserved_video_s=0.0,
            target_word=target_word,
            prompt=prompt,
        )


# --------------------------------------------------------------------------- #
# Shot field helpers (tolerant of the real ORM row and test doubles)
# --------------------------------------------------------------------------- #


def _shot_start(shot: SchedulerShot) -> int:
    """The shot's span start word index (from ``word_index_start`` or source_span)."""
    direct = getattr(shot, "word_index_start", None)
    if direct is not None:
        return int(direct)
    span = shot.source_span or {}
    rng = span.get("word_range")
    if isinstance(rng, (list, tuple)) and rng:
        return int(rng[0])
    return 0


def _shot_duration(shot: SchedulerShot) -> float:
    return float(shot.duration_s or 5.0)


def _shot_prompt(shot: SchedulerShot) -> str | None:
    return getattr(shot, "prompt", None)


def _dedup_key(book_id: str, shot: SchedulerShot) -> str:
    """Idempotency key for a promotion: the content hash if known, else shot id.

    Both are book-global, so two sessions promoting the same shot collapse to one
    render (cross-session dedup, §12.3) and never double-spend the budget.
    """
    shot_hash = getattr(shot, "shot_hash", None)
    if shot_hash:
        return str(shot_hash)
    return f"shot:{book_id}:{shot.id}"


__all__ = [
    "IDLE_PAUSE_MS",
    "BudgetGate",
    "KeyframeMaintainer",
    "QueueKeyframeMaintainer",
    "RenderQueue",
    "SchedulerService",
    "SchedulerShot",
    "SchedulerTick",
    "ShotSource",
]
