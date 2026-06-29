"""Assembly: build a fully-wired :class:`CommandBus` over an :class:`EventStore`.

This is the composition seam for facet B. Given an event store (the in-memory
fake in tests; facet A's Postgres adapter in production), :func:`build_command_bus`
returns a bus with:

* a :class:`Repository` per aggregate kind, each with its blank-aggregate factory;
* every command registered to its handler;
* the standard middleware pipeline (logging -> validation -> auth seam ->
  idempotency), outermost first;
* the deterministic §9.7/§5.4 saga triggers registered on the dispatcher.

Composition can override the auth policy, the idempotency store, the retry policy,
the clock/id factory (for deterministic tests), and the saga sink (inline vs.
enqueue). Nothing here opens a socket — it is pure wiring over the injected store.
"""

from __future__ import annotations

from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain import handlers as h
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.bus import Clock, CommandBus, IdFactory
from app.eventsourcing.domain.canon import CanonEntityAggregate
from app.eventsourcing.domain.concurrency import RetryPolicy, Sleeper
from app.eventsourcing.domain.events import now_utc
from app.eventsourcing.domain.middleware import (
    AuthorizationMiddleware,
    AuthPolicy,
    IdempotencyMiddleware,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    LoggingMiddleware,
    Middleware,
    ValidationMiddleware,
    allow_all,
)
from app.eventsourcing.domain.projection import ProjectionManager, make_projection_sink
from app.eventsourcing.domain.render_shot import RenderShotAggregate
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.saga import SagaDispatcher, SagaSink
from app.eventsourcing.domain.sagas_catalog import register_default_sagas
from app.eventsourcing.domain.session import SessionAggregate
from app.eventsourcing.domain.snapshotting import SnapshotPolicy
from app.eventsourcing.domain.validators import register_default_validators
from app.eventsourcing.store.protocol import EventStore
from app.eventsourcing.store.snapshots import SnapshotStore


def _uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def build_command_bus(
    store: EventStore,
    *,
    auth_policy: AuthPolicy = allow_all,
    idempotency_store: IdempotencyStore | None = None,
    retry_policy: RetryPolicy | None = None,
    saga_sink: SagaSink | None = None,
    extra_middleware: list[Middleware] | None = None,
    snapshot_store: SnapshotStore | None = None,
    snapshot_policy: SnapshotPolicy | None = None,
    projections: ProjectionManager | None = None,
    clock: Clock = now_utc,
    id_factory: IdFactory = _uuid_hex,
    sleeper: Sleeper | None = None,
) -> CommandBus:
    """Return a fully-wired command bus over ``store``.

    Args:
        store: the event store (facet A or the in-memory fake).
        auth_policy: the auth seam policy (defaults permissive; composition wires RBAC).
        idempotency_store: where handled keys are remembered (defaults in-memory).
        retry_policy: optimistic-concurrency backoff (defaults to 3 attempts).
        saga_sink: where triggered follow-up commands go (None -> they are returned
            on the dispatch but not re-run; composition supplies an inline or
            enqueueing sink).
        extra_middleware: appended *inside* the standard pipeline (before the handler).
        snapshot_store: optional snapshot persistence; when set, every repository
            loads from a snapshot + tail and writes snapshots per ``snapshot_policy``.
        snapshot_policy: when to snapshot (defaults to every 50 events).
        projections: optional CQRS read-side; when set, every committed event
            batch is folded into its registered read models inline.
        clock / id_factory / sleeper: injectable for deterministic tests.
    """
    policy = snapshot_policy or SnapshotPolicy()

    def _repo(factory: object) -> Repository[AggregateRoot]:
        return Repository(
            store,
            factory,  # type: ignore[arg-type]
            snapshot_store=snapshot_store,
            snapshot_policy=policy,
        )

    sessions = _repo(SessionAggregate)
    shots = _repo(RenderShotAggregate)
    canon = _repo(CanonEntityAggregate)

    validation = ValidationMiddleware()
    register_default_validators(validation)

    pipeline: list[Middleware] = [
        LoggingMiddleware(),
        validation,
        AuthorizationMiddleware(policy=auth_policy),
        IdempotencyMiddleware(store=idempotency_store or InMemoryIdempotencyStore()),
    ]
    if extra_middleware:
        pipeline.extend(extra_middleware)

    sagas = register_default_sagas(SagaDispatcher(sink=saga_sink))

    projection_sink = make_projection_sink(projections) if projections is not None else None

    bus = CommandBus(
        store=store,
        middleware=pipeline,
        retry_policy=retry_policy or RetryPolicy(),
        sagas=sagas,
        projection_sink=projection_sink,
        clock=clock,
        id_factory=id_factory,
        sleeper=sleeper,
    )

    # Session
    bus.register(cc.StartSession.command_type, h.handle_start_session, sessions)
    bus.register(cc.UpdateIntent.command_type, h.handle_update_intent, sessions)
    bus.register(cc.SwitchMode.command_type, h.handle_switch_mode, sessions)
    bus.register(cc.LeaveDirectorComment.command_type, h.handle_leave_comment, sessions)
    bus.register(cc.RecordPreference.command_type, h.handle_record_preference, sessions)
    bus.register(cc.EndSession.command_type, h.handle_end_session, sessions)

    # Render-shot
    bus.register(cc.PlanShot.command_type, h.handle_plan_shot, shots)
    bus.register(cc.KeyframeShot.command_type, h.handle_keyframe_shot, shots)
    bus.register(cc.PromoteShot.command_type, h.handle_promote_shot, shots)
    bus.register(cc.RenderShot.command_type, h.handle_render_shot, shots)
    bus.register(cc.ScoreShotQA.command_type, h.handle_score_qa, shots)
    bus.register(cc.RepairShot.command_type, h.handle_repair_shot, shots)
    bus.register(cc.RegenerateShot.command_type, h.handle_regenerate_shot, shots)
    bus.register(cc.ResolveShotConflict.command_type, h.handle_resolve_conflict, shots)

    # Canon
    bus.register(cc.RegisterCanonEntity.command_type, h.handle_register_canon, canon)
    bus.register(cc.EditCanonField.command_type, h.handle_edit_canon_field, canon)
    bus.register(cc.SwapReferenceImage.command_type, h.handle_swap_reference, canon)
    bus.register(cc.EvolveCanonFromConflict.command_type, h.handle_evolve_canon, canon)

    return bus


__all__ = ["build_command_bus"]
