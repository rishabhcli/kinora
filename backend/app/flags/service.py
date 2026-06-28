"""``FlagService`` — the orchestration layer over store + cache + evaluator.

This is the single object the API and the DI container hold. It:

* serves evaluations from the in-memory :class:`~app.flags.cache.FlagCache`
  (zero-I/O on the hot path), reloading the snapshot from
  :class:`~app.flags.store.FlagStore` when stale or on a streamed invalidation;
* writes flags/experiments through the store (versioned + audited) and then
  publishes a cache invalidation so every process refetches;
* assigns experiment arms and persists the resulting exposures durably and
  idempotently (the §13 exposure log);
* exposes a :class:`~app.flags.client.FlagsClient` built over the current
  snapshot for callers that want the ergonomic SDK surface.

It takes a ``session_factory`` (the project's unit-of-work) and an optional
:class:`~app.redis.client.RedisClient`. With neither, the :func:`build_local_service`
helper builds a fully in-memory service for tests / infra-free embeds.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.flags.cache import DEFAULT_CHANNEL, FlagCache
from app.flags.client import FlagsClient
from app.flags.context import EvalContext
from app.flags.evaluator import FlagEvaluator
from app.flags.experiment import Assignment, Experiment, ExperimentEngine
from app.flags.models import EMPTY_SNAPSHOT, Evaluation, Flag, FlagSnapshot
from app.flags.store import ExperimentStore, FlagStore
from app.redis.client import RedisClient

logger = get_logger("app.flags.service")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class FlagService:
    """Stateful façade coordinating persistence, caching, and evaluation."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        redis: RedisClient | None = None,
        default_salt: str = "",
        cache_ttl_s: float = 30.0,
        channel: str = DEFAULT_CHANNEL,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._default_salt = default_salt
        self._cache = FlagCache(
            self._load_snapshot, redis=redis, ttl_s=cache_ttl_s, channel=channel
        )
        self._experiments: dict[str, ExperimentEngine] = {}
        self._experiments_loaded = False

    # --- snapshot loading ---------------------------------------------- #

    async def _load_snapshot(self) -> FlagSnapshot:
        async with self._session_factory() as session:
            store = FlagStore(session)
            # Use the remote invalidation version as the snapshot version so the
            # cache and all peers agree on freshness even across restarts.
            remote = await self._cache.remote_version()
            return await store.load_snapshot(version=remote or 0)

    async def snapshot(self, *, force: bool = False) -> FlagSnapshot:
        """The current (possibly reloaded) flag snapshot."""
        return await self._cache.get(force=force)

    async def evaluator(self, *, force: bool = False) -> FlagEvaluator:
        """A :class:`FlagEvaluator` over the current snapshot."""
        snap = await self.snapshot(force=force)
        return FlagEvaluator(snap, default_salt=self._default_salt)

    async def client(self, *, force: bool = False) -> FlagsClient:
        """A :class:`FlagsClient` SDK over the current snapshot + experiments."""
        snap = await self.snapshot(force=force)
        await self._ensure_experiments()
        # The SDK client returned here is read-only (no exposure sink): durable
        # exposure logging happens through the async :meth:`assign` path, which
        # the sync client accessors cannot do. Callers wanting logged assignment
        # should use :meth:`assign` directly.
        return FlagsClient(
            snap,
            experiments=tuple(e.experiment for e in self._experiments.values()),
            default_salt=self._default_salt,
        )

    # --- evaluation ----------------------------------------------------- #

    async def evaluate(
        self, flag_key: str, context: EvalContext, *, default: Any = None
    ) -> Evaluation:
        """Evaluate one flag against the current snapshot."""
        evaluator = await self.evaluator()
        return evaluator.evaluate(flag_key, context, default=default)

    async def evaluate_all(self, context: EvalContext) -> dict[str, Evaluation]:
        """Evaluate every (non-archived) flag for a context — the SDK bootstrap call."""
        snap = await self.snapshot()
        evaluator = FlagEvaluator(snap, default_salt=self._default_salt)
        return {
            key: evaluator.evaluate(key, context)
            for key, flag in snap.flags.items()
            if not flag.archived
        }

    # --- flag authoring (writes invalidate the cache) ------------------ #

    async def upsert_flag(self, flag: Flag, *, actor: str | None = None) -> Flag:
        """Persist a flag (versioned + audited) and invalidate every cache."""
        async with self._session_factory() as session:
            saved = await FlagStore(session).save(flag, actor=actor)
        await self._invalidate()
        return saved

    async def set_enabled(self, key: str, enabled: bool, *, actor: str | None = None) -> Flag:
        """Flip a flag's kill switch and invalidate."""
        async with self._session_factory() as session:
            saved = await FlagStore(session).set_enabled(key, enabled, actor=actor)
        await self._invalidate()
        return saved

    async def archive_flag(self, key: str, *, actor: str | None = None) -> Flag:
        async with self._session_factory() as session:
            saved = await FlagStore(session).archive(key, actor=actor)
        await self._invalidate()
        return saved

    async def delete_flag(self, key: str, *, actor: str | None = None) -> bool:
        async with self._session_factory() as session:
            existed = await FlagStore(session).delete(key, actor=actor)
        await self._invalidate()
        return existed

    async def list_flags(self, *, include_archived: bool = False) -> list[Flag]:
        async with self._session_factory() as session:
            return await FlagStore(session).list_all(include_archived=include_archived)

    async def get_flag(self, key: str) -> Flag | None:
        async with self._session_factory() as session:
            return await FlagStore(session).get(key)

    async def audit_log(
        self, *, subject_key: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = await FlagStore(session).audit_log(subject_key=subject_key, limit=limit)
            return [
                {
                    "subject_kind": r.subject_kind,
                    "subject_key": r.subject_key,
                    "action": r.action,
                    "actor": r.actor,
                    "summary": r.summary,
                    "changes": r.changes,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    # --- experiments ---------------------------------------------------- #

    async def upsert_experiment(
        self, experiment: Experiment, *, actor: str | None = None
    ) -> Experiment:
        async with self._session_factory() as session:
            saved = await ExperimentStore(session).save(experiment, actor=actor)
        self._experiments_loaded = False  # force a reload of the registry
        return saved

    async def list_experiments(self) -> list[Experiment]:
        async with self._session_factory() as session:
            return await ExperimentStore(session).list_all()

    async def get_experiment(self, key: str) -> Experiment | None:
        async with self._session_factory() as session:
            return await ExperimentStore(session).get(key)

    async def assign(self, experiment_key: str, context: EvalContext) -> Assignment | None:
        """Assign a context to an experiment and durably log the exposure once."""
        await self._ensure_experiments()
        engine = self._experiments.get(experiment_key)
        if engine is None:
            return None
        assignment = engine.assign(context)
        if assignment.in_experiment and not context.anonymous:
            dedup_key = engine.exposure_key(context, assignment)
            if dedup_key is not None and assignment.variant_key is not None:
                async with self._session_factory() as session:
                    await ExperimentStore(session).log_exposure(
                        experiment_key=experiment_key,
                        experiment_version=assignment.experiment_version,
                        variant_key=assignment.variant_key,
                        unit_key=context.unit_for(engine.experiment.bucket_by),
                        dedup_key=dedup_key,
                        context=context.to_dict(),
                    )
        return assignment

    async def exposure_counts(self, experiment_key: str) -> dict[str, int]:
        async with self._session_factory() as session:
            return await ExperimentStore(session).exposure_counts(experiment_key)

    async def decide_experiment(
        self, experiment_key: str, observations: dict[str, dict[str, Any]], *, alpha: float = 0.05
    ) -> dict[str, Any] | None:
        """Build the ship/hold/rollback decision report from supplied metric data.

        ``observations`` is ``{variant_key: {metric_key: {"successes": int,
        "trials": int}}}`` — the per-arm proportion observations your metric
        pipeline produces (the §13 CCS / regen-rate numbers). Returns the report
        dict, or ``None`` if the experiment is unknown.
        """
        from app.flags.report import Observations, build_report
        from app.flags.stats import ProportionStat

        experiment = await self.get_experiment(experiment_key)
        if experiment is None:
            return None
        obs: Observations = {
            variant: {
                metric: ProportionStat(int(v["successes"]), int(v["trials"]))
                for metric, v in metrics.items()
            }
            for variant, metrics in observations.items()
        }
        return build_report(experiment, obs, alpha=alpha).to_dict()

    # --- internals ------------------------------------------------------ #

    async def _ensure_experiments(self) -> None:
        if self._experiments_loaded:
            return
        experiments = await self.list_experiments()
        self._experiments = {e.key: ExperimentEngine(e) for e in experiments}
        self._experiments_loaded = True

    async def _invalidate(self) -> None:
        await self._cache.publish_invalidation()
        await self._cache.reload()


# --------------------------------------------------------------------------- #
# In-memory service for tests / infra-free embeds
# --------------------------------------------------------------------------- #


class InMemoryFlagService:
    """A fully in-memory FlagService-shaped object (no DB, no Redis).

    Useful for tests and embeds that want the service surface (evaluate, assign,
    upsert) without standing up Postgres. Flags/experiments live in dicts;
    exposures are de-duplicated in a set.
    """

    def __init__(self, *, default_salt: str = "") -> None:
        self._flags: dict[str, Flag] = {}
        self._experiments: dict[str, Experiment] = {}
        self._exposures: set[str] = set()
        self._default_salt = default_salt
        self._version = 0

    def _snapshot(self) -> FlagSnapshot:
        return (
            FlagSnapshot.from_flags(tuple(self._flags.values()), version=self._version)
            if self._flags
            else EMPTY_SNAPSHOT
        )

    async def evaluate(
        self, flag_key: str, context: EvalContext, *, default: Any = None
    ) -> Evaluation:
        evaluator = FlagEvaluator(self._snapshot(), default_salt=self._default_salt)
        return evaluator.evaluate(flag_key, context, default=default)

    async def upsert_flag(self, flag: Flag, *, actor: str | None = None) -> Flag:
        version = (self._flags[flag.key].version + 1) if flag.key in self._flags else 1
        saved = flag.with_version(version)
        self._flags[flag.key] = saved
        self._version += 1
        return saved

    async def get_flag(self, key: str) -> Flag | None:
        return self._flags.get(key)

    async def upsert_experiment(
        self, experiment: Experiment, *, actor: str | None = None
    ) -> Experiment:
        self._experiments[experiment.key] = experiment
        return experiment

    async def get_experiment(self, key: str) -> Experiment | None:
        return self._experiments.get(key)

    async def assign(self, experiment_key: str, context: EvalContext) -> Assignment | None:
        exp = self._experiments.get(experiment_key)
        if exp is None:
            return None
        engine = ExperimentEngine(exp)
        assignment = engine.assign(context)
        if assignment.in_experiment and not context.anonymous:
            key = engine.exposure_key(context, assignment)
            if key is not None:
                self._exposures.add(key)
        return assignment

    async def exposure_counts(self, experiment_key: str) -> dict[str, int]:
        # Reconstruct per-variant counts from the de-dup keys we kept.
        counts: dict[str, int] = {}
        exp = self._experiments.get(experiment_key)
        if exp is None:
            return counts
        engine = ExperimentEngine(exp)
        for dedup in self._exposures:
            if not dedup.startswith(f"{experiment_key}:"):
                continue
            unit = dedup.rsplit(":", 1)[-1]
            a = engine.assign(EvalContext.of(unit))
            if a.variant_key is not None:
                counts[a.variant_key] = counts.get(a.variant_key, 0) + 1
        return counts

    async def decide_experiment(
        self, experiment_key: str, observations: dict[str, dict[str, Any]], *, alpha: float = 0.05
    ) -> dict[str, Any] | None:
        from app.flags.report import Observations, build_report
        from app.flags.stats import ProportionStat

        exp = self._experiments.get(experiment_key)
        if exp is None:
            return None
        obs: Observations = {
            variant: {
                metric: ProportionStat(int(v["successes"]), int(v["trials"]))
                for metric, v in metrics.items()
            }
            for variant, metrics in observations.items()
        }
        return build_report(exp, obs, alpha=alpha).to_dict()


def build_local_service(*, default_salt: str = "") -> InMemoryFlagService:
    """Build an infra-free in-memory service (tests / embeds)."""
    return InMemoryFlagService(default_salt=default_salt)


__all__ = ["FlagService", "InMemoryFlagService", "build_local_service"]
