"""Local Wan2.2 (TI2V-5B) video provider — a drop-in for the cloud VideoProvider.

TEMPORARY dev backend. Calls a host-side inference server (native macOS / MPS)
over HTTP; selected when ``settings.video_backend == "local"``. Revert to cloud
Wan by setting ``VIDEO_BACKEND=cloud`` (the default) — nothing else changes.

Differences from the cloud :class:`~app.providers.video.VideoProvider`:
  * does NOT gate on ``kinora_live_video`` — local renders are free, so when this
    backend is selected we always render;
  * image-conditioned modes (i2v / first-last-frame / reference) currently fall
    back to text-to-video (prompt only); continuity rides the prompt for now.
"""
from __future__ import annotations

import base64
import uuid

import httpx

from app.core.config import Settings

from .types import VideoResult, WanSpec

# Kinora films are vertical short-form; the host server scales/pads to this.
_TARGET_W = 720
_TARGET_H = 1280
_MODEL_TAG = "wan2.2-ti2v-5b@local"


class LocalWanVideoProvider:
    """Render clips on a local Wan2.2 host server instead of cloud DashScope."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = settings.local_wan_url.rstrip("/")
        # MPS generation is slow (minutes per clip); allow a generous read ceiling.
        self._timeout = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)

    async def render(self, spec: WanSpec) -> VideoResult:
        payload = {
            "prompt": spec.prompt,
            "negative_prompt": spec.negative_prompt,
            "seed": spec.seed,
            "target_width": _TARGET_W,
            "target_height": _TARGET_H,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._url}/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()

        clip_bytes = base64.b64decode(data["video_b64"])
        last_frame_b64 = data.get("last_frame_b64")
        last_frame_bytes = base64.b64decode(last_frame_b64) if last_frame_b64 else None
        return VideoResult(
            duration_s=float(data.get("duration_s", spec.duration_s)),
            model=_MODEL_TAG,
            mode=spec.mode,
            provider_task_id=f"local-{uuid.uuid4().hex[:12]}",
            clip_url=None,
            clip_bytes=clip_bytes,
            last_frame_bytes=last_frame_bytes,
        )

    async def verify_model_available(self, model: str | None = None) -> bool:
        """Cheap health probe; never raises (safe in the boot path)."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                r = await client.get(f"{self._url}/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


__all__ = ["LocalWanVideoProvider"]
