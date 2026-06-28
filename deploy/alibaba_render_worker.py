#!/usr/bin/env python3
"""Kinora proof-of-deployment artifact for Alibaba Cloud (kinora.md §12.6).

This is the **real, runnable** Alibaba Cloud render worker — the deployable form
of the spec's §12.6 sketch. It runs on **ECS / Function Compute** as a long-lived
**queue worker** (§12.1): it consumes the Redis priority render queue and, for
each claimed shot, renders through the production pipeline — calling **DashScope /
Model Studio** (hosted Wan video synthesis, CosyVoice narration, the Qwen crew) and
persisting clips/keyframes/audio to **OSS** object storage.

It deliberately **reuses the application's real code** rather than duplicating
logic, so this file is an honest proof, not a parallel implementation:

* ``app.storage.object_store.ObjectStore`` — the one boto3 S3v4 client used
  everywhere; pointed at OSS's S3-compatible endpoint in production (MinIO
  locally). This is the OSS integration.
* ``app.providers.video.VideoProvider`` — the real DashScope hosted Wan async
  video-synthesis client (submit → poll → fetch). This is the DashScope
  integration. It is gated by ``KINORA_LIVE_VIDEO`` so deploying never silently
  spends video-seconds.
* ``app.queue.worker.build_worker`` — the real queue consumer + per-shot render
  pipeline (including the ffmpeg degradation ladder when the gate is off).

Two entrypoints:

* ``main()`` runs the **actual deployable worker** (``build_worker().run()``) —
  this is what the ECS instance / FC service executes (see ``deploy/Dockerfile``
  and ``infra/terraform``).
* ``render_shot_to_oss(spec)`` is the **minimal §12.6 demonstration**: it renders
  one shot with hosted DashScope Wan and writes the clip to OSS, mirroring the spec's
  ``render_shot`` signature while using the real providers + ObjectStore.

Run locally (from the repo root)::

    DASHSCOPE_API_KEY=sk-... \
    OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com \
    OSS_AK=... OSS_SECRET=... OSS_BUCKET=kinora-assets \
    REDIS_URL=redis://:pass@<tair-host>:6379/0 \
    DATABASE_URL=postgresql+asyncpg://kinora:pass@<rds-host>:5432/kinora \
    python deploy/alibaba_render_worker.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Any

# Make the backend package importable when run as a loose script from the repo
# root (in the container the backend is already on PYTHONPATH=/app).
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _apply_oss_aliases() -> None:
    """Map the §12.6 ``OSS_*`` env names onto the app's ``S3_*`` settings.

    The app talks to object storage through one S3-compatible boto3 client, so
    OSS is configured exactly like S3/MinIO: an endpoint + access key + bucket.
    This lets the deployment use the spec's ``OSS_AK`` / ``OSS_SECRET`` /
    ``OSS_BUCKET`` / ``OSS_ENDPOINT`` names while the app keeps its ``S3_*`` API.
    Must run before any ``get_settings()`` call (settings are cached).
    """
    aliases = {
        "OSS_ENDPOINT": "S3_ENDPOINT_URL",
        "OSS_AK": "S3_ACCESS_KEY",
        "OSS_SECRET": "S3_SECRET_KEY",
        "OSS_BUCKET": "S3_BUCKET",
        "OSS_REGION": "S3_REGION",
    }
    for src, dst in aliases.items():
        value = os.environ.get(src)
        if value and not os.environ.get(dst):
            os.environ[dst] = value
    # Default to the DashScope intl endpoint (Singapore) unless told otherwise.
    os.environ.setdefault("DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com")


async def render_shot_to_oss(spec: dict[str, Any]) -> dict[str, Any]:
    """Render one shot with hosted DashScope Wan and write the clip to OSS.

    Demonstrably uses **DashScope** (``VideoProvider.render`` → hosted Wan
    video-synthesis) and **OSS** (``ObjectStore.put_bytes`` via the S3-compatible
    endpoint). Honest about spend: the render is gated by ``KINORA_LIVE_VIDEO``;
    when the gate is off ``LiveVideoDisabled`` is raised and **no** Wan task is
    submitted (the queue worker's pipeline degrades to Ken-Burns instead).

    Args:
        spec: ``{shot_id, prompt, negative_prompt?, reference_urls?, seed?,
            target_duration_s?, book_id?, model?}`` — mirrors the §12.6 sample shape.

    Returns:
        ``{clip_url, task_id, video_seconds, model}`` with the ``oss://`` clip URL.
    """
    _apply_oss_aliases()
    from app.core.config import get_settings
    from app.providers import create_providers
    from app.providers.types import WanMode, WanSpec
    from app.storage.object_store import ObjectStore, keys

    settings = get_settings()
    providers = create_providers(settings)
    store = ObjectStore.from_settings(settings)
    try:
        refs = list(spec.get("reference_urls", []))
        wan = WanSpec(
            mode=WanMode.REFERENCE_TO_VIDEO if refs else WanMode.TEXT_TO_VIDEO,
            prompt=str(spec["prompt"]),
            negative_prompt=spec.get("negative_prompt"),
            reference_image_urls=refs,
            seed=spec.get("seed"),
            duration_s=int(spec.get("target_duration_s", 5)),
            shot_id=spec.get("shot_id"),
            model=spec.get("model"),
        )
        result = await providers.video.render(wan)  # → DashScope Wan (Model Studio)
        clip_bytes = result.clip_bytes or b""
        key = keys.clip(str(spec.get("book_id", "demo")), str(spec["shot_id"]))
        await asyncio.to_thread(store.put_bytes, key, clip_bytes, "video/mp4")  # → OSS
        return {
            "clip_url": f"oss://{store.bucket}/{key}",
            "task_id": result.provider_task_id,
            "video_seconds": result.duration_s,
            "model": result.model,
        }
    finally:
        await providers.aclose()


def main() -> int:
    """Run the real ECS / Function Compute render worker (the deployable process)."""
    _apply_oss_aliases()
    from app.core.config import get_settings
    from app.core.logging import configure_logging, get_logger
    from app.queue.worker import build_worker

    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("deploy.alibaba_render_worker")
    logger.info(
        "alibaba_render_worker.start",
        oss_endpoint=settings.s3_endpoint_url,
        oss_bucket=settings.s3_bucket,
        dashscope_base=settings.dashscope_base_url,
        live_video=settings.kinora_live_video,
    )

    async def _run() -> None:
        worker = build_worker(settings=settings)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await worker.run(stop=stop)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
