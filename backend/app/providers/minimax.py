"""Hosted MiniMax (Hailuo) video synthesis (async submit → poll → retrieve →
download) with the KINORA_LIVE_VIDEO spend gate and a hard, persistent USD guard.

Mirrors the Wan ``VideoProvider`` contract (``name`` / ``render(WanSpec)`` /
``healthy()``) so it is a drop-in :class:`~app.providers.video_router.VideoBackend`.
It reuses a :class:`~app.providers.base.ProviderClient` configured for the MiniMax
intl host (its own base URL + bearer key), inheriting retries / breaker /
rate-limit / usage accounting. Real renders burn money, so:

* ``render`` raises :class:`~app.providers.errors.LiveVideoDisabled` before any
  network call when ``settings.kinora_live_video`` is off (belt), and
* a persistent cumulative-USD guard refuses to submit once the next clip would
  cross ``settings.budget_ceiling_usd`` (suspenders), independent of the
  video-seconds ledger.

MiniMax retrieve URLs expire (~9h), so the clip bytes are downloaded immediately
and returned on ``VideoResult.clip_bytes``; the render pipeline persists them to
object storage (it never relies on the expiring URL).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import struct
import time
from typing import Any, Protocol

from .base import ProviderClient  # noqa: F401  # used by Task 3 provider class
from .base import sdk_get as _get  # noqa: F401  # used by Task 3 provider class
from .errors import ProviderError
from .types import (  # noqa: F401  # used by Task 3 provider class
    Usage,
    VideoResult,
    WanMode,
    WanSpec,
)

#: REST paths under ``{minimax_base_url}`` (e.g. https://api.minimax.io/v1).
_SUBMIT_PATH = "video_generation"
_QUERY_PATH = "query/video_generation"
_RETRIEVE_PATH = "files/retrieve"

#: MiniMax task status values.
_STATUS_OK = "Success"
_STATUS_FAIL = "Fail"
_STATUS_PENDING = {"Preparing", "Queueing", "Processing"}

#: Default Redis key for the persistent cumulative-USD spend counter.
_SPEND_KEY = "kinora:minimax:usd_spent"


class MiniMaxBudgetExceeded(ProviderError):  # noqa: N818 - public name in contract
    """Raised when submitting the next clip would cross ``budget_ceiling_usd``.

    A deliberate hard refusal (not a transient fault), so it is non-retryable —
    the router must surface it immediately rather than try another backend.
    """

    retryable = False


def would_exceed_usd(current_usd: float, cost_per_clip_usd: float, ceiling_usd: float) -> bool:
    """True when charging one more clip would push cumulative spend over the cap."""
    return current_usd + cost_per_clip_usd > ceiling_usd


class SpendStore(Protocol):
    """A persistent cumulative-USD counter shared across processes/restarts."""

    async def get_usd(self) -> float:
        """Current cumulative USD spend."""
        ...

    async def add_usd(self, amount: float) -> float:
        """Atomically add ``amount`` USD; return the new cumulative total."""
        ...


class InMemorySpendStore:
    """Process-local :class:`SpendStore` (fallback / tests). Not cross-process."""

    def __init__(self, initial_usd: float = 0.0) -> None:
        self._usd = float(initial_usd)

    async def get_usd(self) -> float:
        return self._usd

    async def add_usd(self, amount: float) -> float:
        self._usd += float(amount)
        return self._usd


class RedisSpendStore:
    """Redis-backed :class:`SpendStore` (production): atomic ``INCRBYFLOAT``.

    Survives restarts and is shared by the separate ``api`` and ``render-worker``
    processes, so neither can independently slip past the USD ceiling.
    """

    def __init__(self, redis: Any, *, key: str = _SPEND_KEY) -> None:
        self._redis = redis
        self._key = key

    async def get_usd(self) -> float:
        raw = await self._redis.get(self._key)
        return float(raw) if raw is not None else 0.0

    async def add_usd(self, amount: float) -> float:
        return float(await self._redis.incrbyfloat(self._key, float(amount)))


#: MiniMax first_frame_image limits.
_MM_MAX_BYTES = 20 * 1024 * 1024
_MM_MIN_SHORT_SIDE = 300
_MM_MIN_ASPECT = 2 / 5  # 0.4
_MM_MAX_ASPECT = 5 / 2  # 2.5
_MM_ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png"}


def _decode_data_uri(image: str) -> tuple[str, bytes] | None:
    """Return ``(mime, raw_bytes)`` for a ``data:`` URI, else ``None``."""
    if not image.startswith("data:"):
        return None
    try:
        header, b64 = image[len("data:"):].split(",", 1)
    except ValueError:
        return None
    mime = header.split(";", 1)[0].strip().lower()
    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, binascii.Error):
        return None
    return mime, raw


def _image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Parse ``(width, height)`` from a PNG or JPEG header, else ``None``."""
    # PNG: signature + IHDR holds width/height as big-endian uint32 at offset 16.
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return int(width), int(height)
    # JPEG: walk the marker segments to the first SOF (Start Of Frame).
    if raw[:2] == b"\xff\xd8":
        i = 2
        n = len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            # SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15 carry dimensions.
            if marker in (
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            ):
                height = struct.unpack(">H", raw[i + 5: i + 7])[0]
                width = struct.unpack(">H", raw[i + 7: i + 9])[0]
                return int(width), int(height)
            seg_len = struct.unpack(">H", raw[i + 2: i + 4])[0]
            i += 2 + seg_len
    return None


def validate_first_frame_image(image: str) -> None:
    """Validate a MiniMax ``first_frame_image`` against the documented rules.

    Rules: JPG/JPEG/PNG; short side > 300px; aspect ratio in [2:5, 5:2]; ≤ 20MB.
    HTTP(S) URLs pass through (MiniMax fetches them; remote dimensions are not
    read here). Only ``data:`` URIs are inspected locally.
    """
    if image.startswith(("http://", "https://")):
        return
    decoded = _decode_data_uri(image)
    if decoded is None:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            "MiniMax first_frame_image must be an http(s) URL or a base64 data URI"
        )
    mime, raw = decoded
    if mime not in _MM_ALLOWED_MIME:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image must be JPG/JPEG/PNG, got {mime!r}"
        )
    if len(raw) > _MM_MAX_BYTES:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image exceeds 20MB ({len(raw)} bytes)"
        )
    dims = _image_dimensions(raw)
    if dims is None:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest("MiniMax first_frame_image dimensions could not be parsed")
    width, height = dims
    if min(width, height) <= _MM_MIN_SHORT_SIDE:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image short side must be > {_MM_MIN_SHORT_SIDE}px "
            f"(got {width}x{height})"
        )
    aspect = width / height if height else 0.0
    if not (_MM_MIN_ASPECT <= aspect <= _MM_MAX_ASPECT):
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image aspect ratio must be in [2:5, 5:2] "
            f"(got {width}x{height} = {aspect:.2f})"
        )


def normalize_first_frame_image(image: str) -> str:
    """Validate and return a MiniMax-acceptable ``first_frame_image`` (the single
    submit-time choke point). Today validation-only; future normalization (e.g.
    transcoding a WEBP keyframe to JPEG) would happen here."""
    validate_first_frame_image(image)
    return image


class MiniMaxVideoProvider:
    """Hosted MiniMax (Hailuo) render client (gated + USD-capped).

    Satisfies the :class:`~app.providers.video_router.VideoBackend` protocol
    (``name`` / ``render`` / ``healthy``) so it is a drop-in alternative to the
    Wan :class:`~app.providers.video.VideoProvider`.
    """

    def __init__(
        self,
        client: ProviderClient,
        *,
        spend_store: SpendStore | None = None,
        name: str | None = None,
        poll_interval_s: float = 10.0,
        poll_timeout_s: float = 600.0,
    ) -> None:
        self._client = client
        self._settings = client.settings
        self._spend = spend_store or InMemorySpendStore()
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self.name = name or f"minimax:{self._settings.minimax_video_model}"

    # -- liveness (no render spend) -------------------------------------- #

    async def healthy(self) -> bool:
        """Cheap probe: no network when the live gate is off (gate != fault)."""
        return True

    # -- request shape --------------------------------------------------- #

    def _submit_body(self, spec: WanSpec) -> dict[str, Any]:
        """Translate a :class:`WanSpec` into the MiniMax submit JSON.

        TEXT_TO_VIDEO -> {model, prompt, duration, resolution}. Image-conditioned
        modes add ``first_frame_image`` (a public URL or a ``data:`` URI). All
        non-t2v modes map to image-to-video using the spec's first available
        image input (MiniMax has no multi-reference / first-last / continuation
        protocol here).
        """
        s = self._settings
        body: dict[str, Any] = {
            "model": s.minimax_video_model,
            "prompt": spec.prompt or "",
            "duration": s.minimax_duration_s,
            "resolution": s.minimax_resolution,
        }
        if spec.mode is not WanMode.TEXT_TO_VIDEO:
            first = self._first_frame(spec)
            if first is None:
                from .errors import ProviderBadRequest

                raise ProviderBadRequest(
                    f"MiniMax {spec.mode.value} render has no first_frame_image input"
                )
            body["first_frame_image"] = normalize_first_frame_image(first)
        return body

    @staticmethod
    def _first_frame(spec: WanSpec) -> str | None:
        """Pick the single conditioning image for image-to-video, by mode."""
        if spec.image_url:
            return spec.image_url
        if spec.first_frame_url:
            return spec.first_frame_url
        if spec.reference_image_urls:
            return spec.reference_image_urls[0]
        return None

    @staticmethod
    def _map_status(status: str) -> str:
        if status == _STATUS_OK:
            return "ok"
        if status == _STATUS_FAIL:
            return "fail"
        return "pending"

    # -- render (GATED + USD-CAPPED) ------------------------------------- #

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit a real MiniMax render, poll, retrieve, download, and return it.

        Order of guards (cheapest/most-deliberate first, no spend until the last):
        1. ``LiveVideoDisabled`` when ``kinora_live_video`` is off (no network).
        2. ``MiniMaxBudgetExceeded`` when the next clip would cross the USD cap.
        Only then is the task submitted.
        """
        if not self._settings.kinora_live_video:
            from .errors import LiveVideoDisabled

            raise LiveVideoDisabled(
                "live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                "no MiniMax task submitted",
            )

        cost = float(self._settings.minimax_cost_per_clip_usd)
        current = await self._spend.get_usd()
        if would_exceed_usd(current, cost, float(self._settings.budget_ceiling_usd)):
            raise MiniMaxBudgetExceeded(
                f"MiniMax USD ceiling would be exceeded: spent ${current:.2f} "
                f"+ ${cost:.2f} > cap ${self._settings.budget_ceiling_usd:.2f}; "
                "refusing to submit",
            )

        task_id = await self._submit(spec)
        # Charge the USD spend as soon as the task is accepted (it is now billable);
        # the video-seconds ledger is charged via record_usage below.
        await self._spend.add_usd(cost)

        file_id = await self._poll_to_completion(task_id)
        download_url = await self._retrieve_download_url(file_id)
        clip_bytes = await self._client.download(download_url, op="video")

        duration = float(self._settings.minimax_duration_s)
        self._client.record_usage(
            Usage(
                model=self._settings.minimax_video_model,
                operation="video",
                video_seconds=duration,
                request_id=task_id,
            )
        )
        return VideoResult(
            duration_s=duration,
            model=self._settings.minimax_video_model,
            mode=spec.mode,
            provider_task_id=task_id,
            clip_url=download_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
        )

    async def _submit(self, spec: WanSpec) -> str:
        body = self._submit_body(spec)
        result = await self._client.request_json(
            "POST",
            f"{self._client.base_url}/{_SUBMIT_PATH}",
            op="minimax_video_submit",
            model=self._settings.minimax_video_model,
            json=body,
        )
        task_id = _get(result, "task_id")
        if not task_id:
            raise ProviderError(
                "MiniMax submission returned no task_id",
                request_id=str(_get(_get(result, "base_resp"), "status_code") or ""),
            )
        return str(task_id)

    async def _poll_to_completion(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout_s
        while True:
            result = await self._client.request_json(
                "GET",
                f"{self._client.base_url}/{_QUERY_PATH}",
                op="minimax_video_poll",
                model=self._settings.minimax_video_model,
                params={"task_id": task_id},
            )
            mapped = self._map_status(str(_get(result, "status") or ""))
            if mapped == "ok":
                file_id = _get(result, "file_id")
                if not file_id:
                    raise ProviderError(
                        "MiniMax task succeeded but returned no file_id",
                        request_id=task_id,
                    )
                return str(file_id)
            if mapped == "fail":
                raise ProviderError(
                    f"MiniMax task {task_id} ended Fail", request_id=task_id
                )
            if time.monotonic() >= deadline:
                from .errors import ProviderTimeout

                raise ProviderTimeout(
                    f"MiniMax task {task_id} did not complete within {self._poll_timeout_s}s",
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _retrieve_download_url(self, file_id: str) -> str:
        result = await self._client.request_json(
            "GET",
            f"{self._client.base_url}/{_RETRIEVE_PATH}",
            op="minimax_file_retrieve",
            model=self._settings.minimax_video_model,
            params={"file_id": file_id},
        )
        url = _get(_get(result, "file"), "download_url")
        if not url:
            raise ProviderError(
                "MiniMax file retrieve returned no download_url", request_id=file_id
            )
        return str(url)


__all__ = [
    "InMemorySpendStore",
    "MiniMaxBudgetExceeded",
    "MiniMaxVideoProvider",
    "RedisSpendStore",
    "SpendStore",
    "normalize_first_frame_image",
    "validate_first_frame_image",
    "would_exceed_usd",
]
