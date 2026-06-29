"""Unit tests for the MiniMax (Hailuo) video provider: the spend guard + store,
the LiveVideoDisabled gate, submit bodies (t2v + i2v), poll status mapping,
retrieve→download, a mocked full success path, image validation. No network."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.minimax import (
    InMemorySpendStore,
    MiniMaxBudgetExceeded,
    would_exceed_usd,
)

# --------------------------------------------------------------------------- #
# Spend guard math + store
# --------------------------------------------------------------------------- #


def test_would_exceed_usd_at_and_below_ceiling() -> None:
    # 157 clips * 0.19 = 29.83 ≤ 30.0 → the 157th is allowed; the 158th crosses.
    assert would_exceed_usd(29.83 - 0.19, 0.19, 30.0) is False  # the 157th
    assert would_exceed_usd(29.83, 0.19, 30.0) is True  # the 158th would be 30.02


async def test_inmemory_spend_store_accumulates() -> None:
    store = InMemorySpendStore()
    assert await store.get_usd() == 0.0
    assert await store.add_usd(0.19) == pytest.approx(0.19)
    assert await store.add_usd(0.19) == pytest.approx(0.38)
    assert await store.get_usd() == pytest.approx(0.38)


def test_minimax_budget_exceeded_is_non_retryable() -> None:
    err = MiniMaxBudgetExceeded("ceiling hit")
    from app.providers.errors import ProviderError

    assert isinstance(err, ProviderError)
    assert err.retryable is False


# --------------------------------------------------------------------------- #
# Provider: gate, submit bodies, poll mapping, success path, usage, USD guard
# --------------------------------------------------------------------------- #

from app.providers.minimax import MiniMaxVideoProvider  # noqa: E402
from app.providers.types import WanMode, WanSpec  # noqa: E402

_FAST = ResilienceConfig(
    max_attempts=2,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
    backoff_jitter_s=0.0,
    breaker_failure_threshold=3,
    breaker_recovery_s=0.05,
    rate_per_s=1000.0,
    rate_burst=1000,
)


def _mm_settings(*, live: bool, ceiling_usd: float = 30.0) -> Settings:
    return Settings(
        dashscope_api_key="test",
        kinora_live_video=live,
        video_backend="minimax",
        minimax_api_key="sk-mm",
        budget_ceiling_usd=ceiling_usd,
    )


def _mm_client(handler: object, *, live: bool, ceiling_usd: float = 30.0) -> ProviderClient:
    return ProviderClient(
        _mm_settings(live=live, ceiling_usd=ceiling_usd),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resilience=_FAST,
        base_url_override="https://api.minimax.io/v1",
        api_key_override="sk-mm",
    )


async def test_render_raises_when_live_video_disabled() -> None:
    called = {"hit": False}

    def _tripwire(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        raise AssertionError("MiniMax endpoint must NOT be called when the gate is off")

    client = _mm_client(_tripwire, live=False)
    provider = MiniMaxVideoProvider(client)
    from app.providers.errors import LiveVideoDisabled

    with pytest.raises(LiveVideoDisabled):
        await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    assert called["hit"] is False
    await client.aclose()


def test_submit_body_text_to_video() -> None:
    client = _mm_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = MiniMaxVideoProvider(client)
    body = provider._submit_body(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn")
    )
    assert body == {
        "model": "MiniMax-Hailuo-2.3-Fast",
        "prompt": "meadow at dawn",
        "duration": 6,
        "resolution": "768P",
    }
    assert "first_frame_image" not in body
    # cleanup is sync-safe; no await needed for the unused client transport


def test_submit_body_image_to_video_sets_first_frame() -> None:
    client = _mm_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = MiniMaxVideoProvider(client)
    body = provider._submit_body(
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            prompt="she turns",
            image_url="https://x/first.jpg",
        )
    )
    assert body["first_frame_image"] == "https://x/first.jpg"
    assert body["model"] == "MiniMax-Hailuo-2.3-Fast"


def test_map_status() -> None:
    assert MiniMaxVideoProvider._map_status("Success") == "ok"
    assert MiniMaxVideoProvider._map_status("Fail") == "fail"
    assert MiniMaxVideoProvider._map_status("Preparing") == "pending"
    assert MiniMaxVideoProvider._map_status("Queueing") == "pending"
    assert MiniMaxVideoProvider._map_status("Processing") == "pending"


async def test_render_success_path_downloads_and_records_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/video_generation") and request.method == "POST":
            return httpx.Response(
                200, json={"task_id": "mm-task-1", "base_resp": {"status_code": 0}}
            )
        if path.endswith("/query/video_generation"):
            assert request.url.params.get("task_id") == "mm-task-1"
            return httpx.Response(200, json={"status": "Success", "file_id": "file-7"})
        if path.endswith("/files/retrieve"):
            assert request.url.params.get("file_id") == "file-7"
            return httpx.Response(
                200, json={"file": {"download_url": "https://cdn.minimax/clip.mp4"}}
            )
        if request.url.host == "cdn.minimax":
            return httpx.Response(200, content=b"MINIMAX-MP4-BYTES")
        return httpx.Response(200, json={})

    client = _mm_client(handler, live=True)
    store = InMemorySpendStore()
    provider = MiniMaxVideoProvider(
        client, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0
    )
    result = await provider.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn")
    )
    assert result.clip_bytes == b"MINIMAX-MP4-BYTES"
    assert result.clip_url == "https://cdn.minimax/clip.mp4"
    assert result.provider_task_id == "mm-task-1"
    assert result.duration_s == 6.0
    assert result.model == "MiniMax-Hailuo-2.3-Fast"
    # video-seconds recorded for the primary budget path
    totals = client.usage_totals
    assert totals is not None and totals.video_seconds == 6.0
    # USD spend persisted
    assert await store.get_usd() == pytest.approx(0.19)
    await client.aclose()


async def test_render_refuses_past_usd_ceiling_and_persists_across_instances() -> None:
    submits = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/video_generation") and request.method == "POST":
            submits["count"] += 1
            return httpx.Response(200, json={"task_id": "t", "base_resp": {"status_code": 0}})
        if path.endswith("/query/video_generation"):
            return httpx.Response(200, json={"status": "Success", "file_id": "f"})
        if path.endswith("/files/retrieve"):
            return httpx.Response(200, json={"file": {"download_url": "https://cdn.minimax/c.mp4"}})
        if request.url.host == "cdn.minimax":
            return httpx.Response(200, content=b"X")
        return httpx.Response(200, json={})

    # Ceiling 0.30 → one 0.19 clip allowed; the second (0.38) is refused.
    store = InMemorySpendStore()
    client1 = _mm_client(handler, live=True, ceiling_usd=0.30)
    p1 = MiniMaxVideoProvider(client1, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0)
    await p1.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="one"))
    assert submits["count"] == 1
    assert await store.get_usd() == pytest.approx(0.19)
    await client1.aclose()

    # A NEW provider instance (simulating a restart) shares the persisted store
    # and refuses BEFORE any submit.
    client2 = _mm_client(handler, live=True, ceiling_usd=0.30)
    p2 = MiniMaxVideoProvider(client2, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0)
    with pytest.raises(MiniMaxBudgetExceeded):
        await p2.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="two"))
    assert submits["count"] == 1  # no second submission happened
    await client2.aclose()


async def test_healthy_is_true_without_network_when_gate_off() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("healthy() must not call the network when gated off")

    client = _mm_client(_tripwire, live=False)
    provider = MiniMaxVideoProvider(client)
    assert await provider.healthy() is True
    assert provider.name == "minimax:MiniMax-Hailuo-2.3-Fast"
    await client.aclose()


# --------------------------------------------------------------------------- #
# first_frame_image validation
# --------------------------------------------------------------------------- #

from app.providers.errors import ProviderBadRequest  # noqa: E402
from app.providers.minimax import (  # noqa: E402
    normalize_first_frame_image,
    validate_first_frame_image,
)


def _png_data_uri(width: int, height: int) -> str:
    """A minimal valid PNG header (IHDR with width/height) as a data URI.

    Only the 24-byte signature+IHDR prefix is needed for dimension parsing; the
    rest is padding so the byte length is realistic but small.
    """
    import base64
    import struct

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    raw = sig + ihdr_len + ihdr + b"\x00" * 64
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_validate_passes_http_url_unchanged() -> None:
    # We cannot read remote dimensions cheaply; URLs pass through.
    validate_first_frame_image("https://x/first.jpg")  # no raise
    assert normalize_first_frame_image("https://x/first.jpg") == "https://x/first.jpg"


def test_validate_passes_valid_png_data_uri() -> None:
    uri = _png_data_uri(800, 600)  # 4:3, short side 600 > 300
    validate_first_frame_image(uri)  # no raise
    assert normalize_first_frame_image(uri) == uri


def test_validate_rejects_short_side_too_small() -> None:
    uri = _png_data_uri(800, 200)  # short side 200 ≤ 300
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image(uri)


def test_validate_rejects_aspect_ratio_out_of_range() -> None:
    uri = _png_data_uri(2000, 400)  # 5:1 aspect → 5.0 > 2.5
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image(uri)


def test_validate_rejects_non_image_data_uri() -> None:
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image("data:text/plain;base64,aGVsbG8=")
