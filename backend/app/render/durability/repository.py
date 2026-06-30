"""Production adapter sketches for the durability stores (kinora.md §9.7, §12.1).

The durability subsystem is defined against narrow Protocols with in-memory impls
(used by every test). This module sketches the **production** adapters that bind
those Protocols to the real infrastructure — Redis for the ephemeral idempotency
claims + checkpoints, Postgres for the stuck-shot scan + the terminal degrade —
without forcing the whole package to import SQLAlchemy/Redis at module load.

Nothing here is on the unit-test path; the adapters are constructed only by the
production wiring (the worker / the recovery role / the container seam). They are
deliberately thin: each one maps the Protocol calls onto an existing repository or
client method, so there is no new schema and no new query logic to maintain — the
``shots`` table and the queue's Redis client already exist.

* :class:`RedisIdempotencyStore` — ``SET NX PX`` claims (atomic, TTL'd, fenced).
* :class:`SqlStuckShotRepo` — the §9.7 stuck-shot scan over ``shots.status`` +
  the terminal-degrade write, reusing :class:`app.db.repositories.shot.ShotRepo`.

The Redis adapter is shown as a documented sketch (the exact Lua/`SET` calls a
production deploy uses); the SQL adapter is concrete because it leans entirely on
the existing repo. Both are exercised only behind the running stack, never in the
no-infra unit suite.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.render.durability.idempotency import ClaimRecord, ClaimState
from app.render.durability.recovery import StuckShot, StuckShotRepo

logger = get_logger("app.render.durability.repository")

__all__ = [
    "RedisIdempotencyStore",
    "SqlStuckShotRepo",
]


@dataclass(slots=True)
class RedisIdempotencyStore:
    """A Redis-backed :class:`~app.render.durability.idempotency.IdempotencyStore`.

    Maps the atomic-claim Protocol onto Redis primitives:

    * ``try_claim`` → ``SET key payload NX PX ttl``. ``NX`` makes the claim atomic
      (only the first caller sets it); ``PX`` makes a crashed holder's claim expire
      so it is reclaimable. Stealing an *expired* claim is just a second ``SET`` once
      the key is gone — Redis evicts it at the PX deadline. A monotone fence comes
      from an ``INCR`` on a sibling counter so :meth:`complete`/:meth:`fail` can
      fence out a stale holder.
    * ``put`` → ``SET`` the terminal/completed payload (no TTL, or a long one) so
      duplicate deliveries resolve to ``COMPLETED``.
    * ``delete`` → ``DEL`` releases a transient-failed claim for a clean retry.

    The implementation below is intentionally a synchronous-shaped sketch against an
    injected client exposing ``set``/``get``/``delete``/``incr``; a real deploy uses
    the project's async Redis client and a small Lua script for the compare-and-set
    on completion. It is **not** exercised by the unit suite (which uses the
    in-memory store) — it documents the production binding.
    """

    client: Any
    fence_key: str = "render:idemp:fence"
    key_prefix: str = "render:idemp:"

    def _k(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    def get(self, key: str) -> ClaimRecord | None:
        raw = self.client.get(self._k(key))
        if not raw:
            return None
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        return ClaimRecord.from_dict(data)

    def try_claim(self, key: str, *, ttl_s: float, now: float) -> ClaimRecord | None:
        existing = self.get(key)
        if existing is not None:
            if existing.state is ClaimState.COMPLETED:
                return None
            if existing.expires_at > now:
                return None
        fence = int(self.client.incr(self.fence_key))
        record = ClaimRecord(
            key=key, state=ClaimState.IN_FLIGHT, fence=fence, expires_at=now + ttl_s
        )
        # NX guards the race between two workers reaching here at once; on a lost
        # race we re-read and the winner's live claim blocks us next call.
        ok = self.client.set(
            self._k(key), json.dumps(record.as_dict()), nx=True, px=int(ttl_s * 1000)
        )
        if not ok:
            logger.info("idempotency.redis_lost_race", key=key)
            return None
        return record

    def put(self, record: ClaimRecord) -> None:
        self.client.set(self._k(record.key), json.dumps(record.as_dict()))

    def delete(self, key: str) -> None:
        self.client.delete(self._k(key))


@dataclass(slots=True)
class SqlStuckShotRepo(StuckShotRepo):
    """A Postgres-backed :class:`StuckShotRepo` over the existing ``shots`` table.

    ``list_stuck`` selects rows whose ``status`` is non-terminal (the §9.7 scan);
    ``mark_degraded`` reuses :meth:`ShotRepo.set_status`. No new schema — the render
    state already lives on ``shots.status``.
    """

    session: Any  # an AsyncSession

    async def list_stuck(
        self, *, statuses: Sequence[ShotStatus], limit: int
    ) -> list[StuckShot]:
        from sqlalchemy import select

        from app.db.models.shot import Shot

        rows = (
            (
                await self.session.execute(
                    select(Shot).where(Shot.status.in_(list(statuses))).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            StuckShot(
                shot_id=row.id,
                book_id=row.book_id,
                status=ShotStatus(row.status),
                scene_id=getattr(row, "scene_id", None),
                beat_id=getattr(row, "beat_id", None),
                shot_hash=getattr(row, "shot_hash", None),
            )
            for row in rows
        ]

    async def mark_degraded(self, shot_id: str) -> None:
        from app.db.repositories.shot import ShotRepo

        await ShotRepo(self.session).set_status(shot_id, ShotStatus.DEGRADED)


# A note on the JSON checkpoint store: production reuses the *existing*
# ``app.render.checkpoint.JsonCheckpointStore`` over the queue's async Redis client
# (``get``/``set``/``delete`` of JSON) — no new adapter is needed here, only the
# wiring that hands that store to the :class:`DurableRenderGuard`.
