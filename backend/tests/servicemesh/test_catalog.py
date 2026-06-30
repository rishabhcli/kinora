"""The concrete Kinora message catalog + an end-to-end producer->consumer flow."""

from __future__ import annotations

from app.servicemesh.catalog import (
    BUFFER_STATE,
    CANON_QUERY,
    CANON_QUERY_RESULT,
    SHOT_PROGRESS,
    SHOT_RENDER_JOB,
    build_seed_registry,
    seed_schemas,
)
from app.servicemesh.compatibility import CompatibilityMode
from app.servicemesh.consumer import ConsumerDispatcher
from app.servicemesh.envelope import MessageEnvelope
from app.servicemesh.roles import ProducerRole, TransportKind


def test_seed_registry_has_all_channels() -> None:
    reg = build_seed_registry()
    assert set(reg.schema_ids()) == {
        SHOT_RENDER_JOB,
        SHOT_PROGRESS,
        BUFFER_STATE,
        CANON_QUERY,
        CANON_QUERY_RESULT,
    }


def test_seed_registry_hashes_verify() -> None:
    reg = build_seed_registry()
    reg.verify_hashes()  # no raise


def test_channel_compatibility_modes() -> None:
    reg = build_seed_registry()
    assert reg.channel(SHOT_RENDER_JOB).compatibility is CompatibilityMode.BACKWARD
    assert reg.channel(SHOT_PROGRESS).compatibility is CompatibilityMode.FULL
    assert reg.channel(BUFFER_STATE).compatibility is CompatibilityMode.FULL


def test_seed_schemas_are_stable_content() -> None:
    # Two independent builds hash identically (deterministic content).
    a = {s.schema_id: s.content_hash() for s, _ in seed_schemas()}
    b = {s.schema_id: s.content_hash() for s, _ in seed_schemas()}
    assert a == b


async def test_end_to_end_render_job_flow() -> None:
    reg = build_seed_registry()
    handled: list[dict] = []
    disp = ConsumerDispatcher(reg)
    disp.register_handler(SHOT_RENDER_JOB, "1.0.0", lambda env, p: handled.append(p))

    job = MessageEnvelope.create(
        schema_id=SHOT_RENDER_JOB,
        schema_version="1.0.0",
        payload={
            "shot_hash": "deadbeef",
            "scene_id": "s1",
            "session_id": "sess1",
            "render_mode": "ken_burns",
        },
        producer_role=ProducerRole.API,
        transport=TransportKind.QUEUE_JOB,
        idempotency_key="deadbeef",
    )
    outcome = await disp.dispatch(job)
    assert outcome.handled
    assert handled[0]["shot_hash"] == "deadbeef"


async def test_progress_event_lineage_from_job() -> None:
    reg = build_seed_registry()
    disp = ConsumerDispatcher(reg)
    received: list[MessageEnvelope] = []
    disp.register_handler(SHOT_PROGRESS, "1.0.0", lambda env, p: received.append(env))

    job = MessageEnvelope.create(
        schema_id=SHOT_RENDER_JOB,
        schema_version="1.0.0",
        payload={
            "shot_hash": "h",
            "scene_id": "s",
            "session_id": "x",
            "render_mode": "live",
        },
        producer_role=ProducerRole.API,
    )
    # The render worker emits a progress event caused by the job.
    progress = job.caused_child(
        schema_id=SHOT_PROGRESS,
        schema_version="1.0.0",
        payload={"shot_hash": "h", "session_id": "x", "stage": "rendering"},
        producer_role=ProducerRole.RENDER_WORKER,
    )
    outcome = await disp.dispatch(progress)
    assert outcome.handled
    assert received[0].trace_id == job.trace_id
    assert received[0].causation_id == job.message_id
