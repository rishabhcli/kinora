"""Stuck-shot recovery loop for the render engine (kinora.md §9.7, §12.1).

Mirrors :mod:`app.ingest.recovery`: a long-running loop (also kickable once at
startup) that finds shots left in a **non-terminal** render state after a worker
restart — ``RENDERING`` / ``QA`` (and optionally ``PROMOTED``) — and repairs them
so the §9.7 machine never strands a shot half-rendered.

For each stuck shot the :class:`ShotRecoveryService` decides, using the same
durability primitives as the live path:

* **poisoned** → route to the dead-letter sink + drop to a degraded card (the shot
  has already crashed too many times; never re-attempt it);
* **resumable** (a mid-flight checkpoint exists) → re-enqueue so a worker resumes
  it from the checkpoint (the guard's :func:`probe_resume` picks it up);
* **stale, no checkpoint** → re-enqueue a fresh render (the idempotency key makes a
  duplicate enqueue a no-op);

Each decision is idempotent and best-effort: a recovery tick never throws, and a
shot that another worker is actively rendering is skipped (its idempotency claim is
live). The repository + re-enqueue + degrade are injected Protocols so the whole
service is unit-testable with in-memory fakes (no DB/Redis), and the production
wiring is a thin adapter sketched in :mod:`app.render.durability.repository`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.enums import ShotStatus
from app.observability import metrics
from app.render.checkpoint import CheckpointStore, InMemoryCheckpointStore
from app.render.poison import PoisonTracker

logger = get_logger("app.render.durability.recovery")

__all__ = [
    "RecoveryAction",
    "RecoveryReport",
    "ReenqueueFn",
    "ShotRecoveryService",
    "StuckShot",
    "StuckShotRepo",
    "main",
    "run_recovery_loop",
]

#: Render states that still need work — a shot stuck here after a restart is the
#: recovery loop's business (terminal ACCEPTED/DEGRADED/CONFLICT are left alone;
#: CONFLICT is parked awaiting the director out-of-band, §7.2).
NON_TERMINAL_STATES: frozenset[ShotStatus] = frozenset(
    {ShotStatus.PROMOTED, ShotStatus.RENDERING, ShotStatus.QA}
)


class RecoveryAction(StrEnum):
    """What the recovery service did with one stuck shot."""

    RESUMED = "resumed"
    REPAIRED = "repaired"  # re-enqueued fresh (no checkpoint to resume from)
    DEAD_LETTERED = "dead_lettered"
    SKIPPED = "skipped"  # actively rendering elsewhere / not actually stuck


@dataclass(slots=True)
class StuckShot:
    """The slice of a stuck shot the recovery service needs."""

    shot_id: str
    book_id: str
    status: ShotStatus
    scene_id: str | None = None
    beat_id: str | None = None
    shot_hash: str | None = None


@dataclass(slots=True)
class RecoveryReport:
    """A per-tick tally of recovery actions (returned for logging/metrics)."""

    scanned: int = 0
    by_action: dict[str, int] = field(default_factory=dict)

    def record(self, action: RecoveryAction) -> None:
        self.by_action[action.value] = self.by_action.get(action.value, 0) + 1

    @property
    def acted(self) -> int:
        """Shots the loop actively resumed / repaired / dead-lettered (not skipped)."""
        return sum(v for k, v in self.by_action.items() if k != RecoveryAction.SKIPPED.value)


class StuckShotRepo(Protocol):
    """Find shots stranded in a non-terminal render state (the recovery scan)."""

    async def list_stuck(
        self, *, statuses: Sequence[ShotStatus], limit: int
    ) -> list[StuckShot]: ...

    async def mark_degraded(self, shot_id: str) -> None:
        """Drop a dead-lettered shot to DEGRADED so its row is terminal."""
        ...


#: Re-enqueue a shot for (re)rendering. Returns True if it was enqueued (False when
#: the idempotent enqueue collapsed onto an existing job — already a no-op).
ReenqueueFn = Callable[[StuckShot], Awaitable[bool]]


@dataclass(slots=True)
class ShotRecoveryService:
    """Decide + apply the recovery action for each stuck shot.

    Attributes:
        repo: the stuck-shot scan + the terminal-degrade write.
        reenqueue: re-enqueue a shot for (re)rendering (idempotent on its key).
        checkpoints: probed to tell a resumable shot from a cold restart.
        poison: a quarantined shot is dead-lettered, never re-enqueued.
        dead_letter: optional callable to record a dead-lettered recovery
            (shot_id, book_id, reason); best-effort.
    """

    repo: StuckShotRepo
    reenqueue: ReenqueueFn
    checkpoints: CheckpointStore = field(default_factory=InMemoryCheckpointStore)
    poison: PoisonTracker = field(default_factory=PoisonTracker)
    dead_letter: Callable[..., Awaitable[None]] | None = None

    async def recover_once(
        self, *, statuses: Sequence[ShotStatus] | None = None, limit: int = 50
    ) -> RecoveryReport:
        """Scan + recover one batch of stuck shots; never raises (best-effort)."""
        scan_statuses = list(statuses or NON_TERMINAL_STATES)
        report = RecoveryReport()
        try:
            stuck = await self.repo.list_stuck(statuses=scan_statuses, limit=limit)
        except Exception as exc:  # noqa: BLE001 - a scan failure must not crash the loop
            logger.warning("render.recovery.scan_failed", error=str(exc))
            return report

        for shot in stuck:
            report.scanned += 1
            try:
                action = await self._recover_shot(shot)
            except Exception as exc:  # noqa: BLE001 - isolate one bad shot
                logger.warning(
                    "render.recovery.shot_failed", shot_id=shot.shot_id, error=str(exc)
                )
                action = RecoveryAction.SKIPPED
            report.record(action)
            metrics.inc_render_recovered(action.value)
        if report.acted:
            logger.info("render.recovery.tick", scanned=report.scanned, **report.by_action)
        return report

    async def _recover_shot(self, shot: StuckShot) -> RecoveryAction:
        # A shot that crossed the poison threshold is dead-lettered, never retried.
        if self.poison.is_poisoned(shot.shot_id):
            await self.repo.mark_degraded(shot.shot_id)
            if self.dead_letter is not None:
                with contextlib.suppress(Exception):
                    await self.dead_letter(
                        shot_id=shot.shot_id, book_id=shot.book_id, reason="poison_on_recovery"
                    )
            logger.warning("render.recovery.dead_lettered", shot_id=shot.shot_id)
            return RecoveryAction.DEAD_LETTERED

        checkpoint = await self.checkpoints.load(shot.shot_id)
        if checkpoint is not None and checkpoint.is_terminal:
            # The render actually finished; the row just never updated. Leave the
            # re-enqueue to reconcile via the terminal-checkpoint skip path.
            logger.info("render.recovery.terminal_checkpoint", shot_id=shot.shot_id)

        enqueued = await self.reenqueue(shot)
        if not enqueued:
            return RecoveryAction.SKIPPED  # idempotent enqueue collapsed: already queued
        return RecoveryAction.RESUMED if checkpoint is not None else RecoveryAction.REPAIRED


# --------------------------------------------------------------------------- #
# Long-running loop + entrypoint (mirrors app/ingest/recovery.py)
# --------------------------------------------------------------------------- #


async def run_recovery_loop(
    *,
    service_factory: Callable[[], ShotRecoveryService],
    interval_s: float,
    limit: int,
    stop: asyncio.Event | None = None,
) -> None:
    """Recover stuck shots on a cadence until ``stop`` is set.

    The service is built per-tick via ``service_factory`` so a fresh DB/Redis unit
    of work is used each pass (mirrors the ingest recovery loop's container usage).
    """
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            service = service_factory()
            report = await service.recover_once(limit=limit)
            logger.info("render.recovery.loop_tick", scanned=report.scanned, acted=report.acted)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any tick error
            logger.warning("render.recovery.loop_error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue


def _build_service_from_container(container: Any) -> ShotRecoveryService:
    """Build the recovery service from the wired container (production path).

    Imported lazily inside :func:`main` so this module stays import-light for
    tests. Delegates to the container's render-recovery seam when present, else
    raises a clear error so the deployment wiring is obvious.
    """
    builder = getattr(container, "build_shot_recovery_service", None)
    if builder is None:
        raise RuntimeError(
            "container has no build_shot_recovery_service(); wire the render recovery "
            "seam (see app/render/durability/repository.py for the adapter sketch)"
        )
    return builder()


def main() -> int:
    """``python -m app.render.durability.recovery`` entrypoint (a render-worker role)."""
    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        from app.composition import build_container

        container = build_container(settings)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await run_recovery_loop(
                service_factory=lambda: _build_service_from_container(container),
                interval_s=_recovery_interval(settings),
                limit=_recovery_limit(settings),
                stop=stop,
            )
        finally:
            with contextlib.suppress(Exception):
                await container.shutdown()

    asyncio.run(_run())
    return 0


def _recovery_interval(settings: Settings) -> float:
    """Reuse the ingest recovery cadence unless a render-specific one is configured."""
    fallback = settings.ingest_recovery_interval_s
    return float(getattr(settings, "render_recovery_interval_s", fallback))


def _recovery_limit(settings: Settings) -> int:
    return int(getattr(settings, "render_recovery_limit", settings.ingest_recovery_limit))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
