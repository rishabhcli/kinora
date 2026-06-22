"""``python -m app.eval.run --book <id>`` — produce the §13 eval report JSON.

The operator entrypoint for the Track-3 proof. It assembles the fixed demo
sequence from an ingested book (its shots, their beats, and the canon's locked
references), runs the crew arm (the real per-shot render pipeline, degradation
path → zero video-seconds) against the single-agent baseline over ``--runs``
runs, and prints the report to stdout (and optionally ``--out``). The report is
cached in Redis under :func:`app.api.routes.metrics.report_cache_key` so
``GET /api/eval/report/{book_id}`` can serve it without re-running.

This is a real, infra-backed tool (it opens Postgres/Redis/object-store/provider
connections), so it runs against a live stack — not in the unit suite. The unit
suite drives the same harness/arms with light doubles instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import anyio
from sqlalchemy import select

from app.composition import Container, build_container
from app.core.logging import configure_logging, get_logger
from app.db.models.shot import Shot, SourceSpanIndex
from app.db.repositories.beat import BeatRepo
from app.eval.harness import (
    CrewArm,
    DemoSequence,
    DemoShot,
    EvalReport,
    embed_locked_refs,
    run_protocol,
)
from app.memory.canon_service import CanonService

logger = get_logger("app.eval.run")

#: Default number of shots in the fixed demo sequence (§15 MVP "3–4 shot lookahead"+).
DEFAULT_MAX_SHOTS = 12


async def build_demo_sequence(
    container: Container, book_id: str, *, max_shots: int
) -> DemoSequence:
    """Assemble the fixed demo sequence + shared locked refs from an ingested book.

    Reads the book's shots in reading order (via the source-span index), resolves
    each shot's beat for its prompt + the canon characters present, and fetches
    each character's locked reference image once (the shared ground truth both
    arms are scored against).
    """
    locked_ref_keys: dict[str, str] = {}
    shots: list[DemoShot] = []
    async with container.session_factory() as db:
        beats = BeatRepo(db)
        canon = CanonService(
            db, embedder=container.providers.embeddings, blob_store=container.object_store
        )
        stmt = (
            select(Shot, SourceSpanIndex.word_index_start)
            .join(SourceSpanIndex, SourceSpanIndex.shot_id == Shot.id)
            .where(SourceSpanIndex.book_id == book_id)
            .order_by(SourceSpanIndex.word_index_start)
            .limit(max_shots)
        )
        rows = list((await db.execute(stmt)).all())
        if not rows:
            raise SystemExit(f"book {book_id!r} has no shots — ingest it first (Phase A)")

        for index, (shot, _start) in enumerate(rows):
            beat_id = shot.beat_id
            character_keys: list[str] = []
            prompt = ""
            if beat_id is not None:
                beat = await beats.get(beat_id)
                if beat is not None:
                    prompt = beat.described_visuals or beat.summary or ""
                slice_ = await canon.query(book_id, beat_id)
                character_keys = [ch.entity_key for ch in slice_.characters]
                for character in slice_.characters:
                    if character.entity_key in locked_ref_keys:
                        continue
                    for ref in character.reference_images:
                        if ref.locked and ref.key:
                            locked_ref_keys[character.entity_key] = ref.key
                            break
            shots.append(
                DemoShot(
                    shot_id=shot.id,
                    scene_id=shot.scene_id or "scene_001",
                    seed=int(shot.seed) if shot.seed else 1000 + index,
                    prompt=prompt or f"shot {index}",
                    character_keys=character_keys,
                    est_duration_s=float(shot.duration_s or 5.0),
                )
            )

    locked_refs: dict[str, bytes] = {}
    for character_key, key in locked_ref_keys.items():
        try:
            locked_refs[character_key] = await anyio.to_thread.run_sync(
                container.object_store.get_bytes, key
            )
        except Exception as exc:  # noqa: BLE001 - a missing ref just drops that character
            logger.warning("eval.locked_ref_missing", character=character_key, error=str(exc))
    return DemoSequence(book_id=book_id, shots=shots, locked_refs=locked_refs)


def _build_crew_arm(container: Container) -> CrewArm:
    """Wire the crew arm over the real render pipeline (degradation → zero video)."""
    from app.render.pipeline import build_render_pipeline

    async def render_shot(book_id: str, shot_id: str) -> Any:
        async with container.session_factory() as db:
            pipeline = build_render_pipeline(
                db,
                providers=container.providers,
                object_store=container.object_store,
                settings=container.settings,
            )
            return await pipeline.render_shot(book_id, shot_id)

    async def get_bytes(key: str) -> bytes:
        return await anyio.to_thread.run_sync(container.object_store.get_bytes, key)

    return CrewArm(
        render_shot=render_shot,
        embedder=container.providers.embeddings,
        get_bytes=get_bytes,
    )


async def run_eval(book_id: str, *, runs: int, max_shots: int, write_cache: bool) -> EvalReport:
    """Run the full §13 protocol for a book and (optionally) cache the report."""
    from app.api.routes.metrics import report_cache_key
    from app.eval.baseline import BaselineArm

    container = build_container()
    try:
        await container.startup()
        sequence = await build_demo_sequence(container, book_id, max_shots=max_shots)
        locked_ref_embeddings = await embed_locked_refs(
            container.providers.embeddings, sequence
        )
        crew = _build_crew_arm(container)
        baseline = BaselineArm(
            chat=container.providers.chat,
            image=container.providers.image,
            embedder=container.providers.embeddings,
            settings=container.settings,
        )
        report = await run_protocol(
            crew=crew,
            baseline=baseline,
            sequence=sequence,
            locked_ref_embeddings=locked_ref_embeddings,
            runs=runs,
        )
        if write_cache:
            await container.redis.set_json(report_cache_key(book_id), report.to_contract())
            logger.info("eval.report_cached", book_id=book_id)
        return report
    finally:
        await container.shutdown()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval.run",
        description="Produce the Kinora §13 crew-vs-baseline eval report for a book.",
    )
    parser.add_argument("--book", required=True, help="the book id to evaluate")
    parser.add_argument("--runs", type=int, default=3, help="runs to average (default 3)")
    parser.add_argument(
        "--shots", type=int, default=DEFAULT_MAX_SHOTS, help="max shots in the demo sequence"
    )
    parser.add_argument("--out", default=None, help="also write the report JSON to this file")
    parser.add_argument(
        "--no-cache", action="store_true", help="do not cache the report in Redis"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: run the eval and emit the report JSON to stdout."""
    args = _parse_args(argv)
    configure_logging("INFO")
    report = asyncio.run(
        run_eval(
            args.book,
            runs=args.runs,
            max_shots=args.shots,
            write_cache=not args.no_cache,
        )
    )
    contract = report.to_contract()
    payload = json.dumps(contract, indent=2)
    print(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    # A terse human summary on stderr (stdout stays clean JSON for piping).
    print(
        f"crew CCS {contract['ccs']['crew']:.3f} vs baseline {contract['ccs']['baseline']:.3f}; "
        f"crew efficiency {contract['efficiency']['crew']:.1f}% vs "
        f"baseline {contract['efficiency']['baseline']:.1f}% "
        f"(runs={contract['runs']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
