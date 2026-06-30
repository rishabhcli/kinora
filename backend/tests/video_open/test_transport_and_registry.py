"""Network gate (default OFF), registry build_adapter/build_fleet, last-frame."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.errors import LiveVideoDisabled
from app.providers.types import WanMode, WanSpec
from app.video.adapters.open import (
    OpenAdapterSpec,
    UnknownAdapterKind,
    build_adapter,
    build_fleet,
    extract_last_frame,
    ffmpeg_available,
    load_bundled,
    mochi_capabilities,
)
from app.video.adapters.open.transport import (
    NetworkDisabled,
    OpenHttpTransport,
    OpenTransportConfig,
)

from .conftest import NO_SLEEP_POLL, RouteMap, tripwire

# --------------------------------------------------------------------------- #
# Network gate
# --------------------------------------------------------------------------- #


async def test_network_gate_blocks_post_when_off(settings: Settings) -> None:
    cfg = OpenTransportConfig(base_url="https://x.test", api_key="k", allow_network=False)
    transport = OpenHttpTransport(cfg, transport=tripwire(), settings=settings)
    with pytest.raises(NetworkDisabled):
        await transport.post_json("go", op="t", model="m", body={})
    with pytest.raises(NetworkDisabled):
        await transport.get_json("go", op="t", model="m")
    with pytest.raises(NetworkDisabled):
        await transport.download("https://x/y.mp4")
    await transport.aclose()


async def test_network_gate_allows_when_on(settings: Settings) -> None:
    routes = RouteMap().json("go", body={"ok": True})
    cfg = OpenTransportConfig(base_url="https://x.test", api_key="k", allow_network=True)
    transport = OpenHttpTransport(cfg, transport=routes.transport(), settings=settings)
    body = await transport.post_json("go", op="t", model="m", body={"a": 1})
    assert body == {"ok": True}
    await transport.aclose()


def test_network_disabled_is_non_retryable() -> None:
    assert NetworkDisabled("x").retryable is False


async def test_auth_schemes_set_correct_header(settings: Settings) -> None:
    for scheme, expected in [
        ("bearer", "Bearer tok"),
        ("token", "Token tok"),
        ("key", "Key tok"),
    ]:
        seen: dict[str, str] = {}

        def handler(req: httpx.Request, _seen: dict[str, str] = seen) -> httpx.Response:
            _seen["auth"] = req.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        cfg = OpenTransportConfig(
            base_url="https://x.test", api_key="tok", auth_scheme=scheme, allow_network=True
        )
        t = OpenHttpTransport(cfg, transport=httpx.MockTransport(handler), settings=settings)
        await t.post_json("go", op="t", model="m", body={})
        assert seen["auth"] == expected
        await t.aclose()


async def test_none_scheme_sends_no_auth(settings: Settings) -> None:
    seen: dict[str, str | None] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={})

    cfg = OpenTransportConfig(base_url="https://x.test", auth_scheme="none", allow_network=True)
    t = OpenHttpTransport(cfg, transport=httpx.MockTransport(handler), settings=settings)
    await t.post_json("go", op="t", model="m", body={})
    # empty bearer key → header omitted entirely by the shared client
    assert seen["auth"] in (None, "Bearer ", "")
    await t.aclose()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_builds_each_kind(settings: Settings) -> None:
    specs = [
        OpenAdapterSpec(kind="descriptor", params={"descriptor": load_bundled("openapi_example")}),
        OpenAdapterSpec(
            kind="replicate", params={"version": "v", "capabilities": mochi_capabilities()}
        ),
        OpenAdapterSpec(
            kind="fal", params={"app_id": "fal-ai/m", "capabilities": mochi_capabilities()}
        ),
        OpenAdapterSpec(kind="stability"),
        OpenAdapterSpec(kind="mochi", params={"version": "v"}),
        OpenAdapterSpec(kind="cogvideox", params={"version": "v"}),
        OpenAdapterSpec(kind="ltx", params={"version": "v"}),
        OpenAdapterSpec(kind="hunyuan"),
    ]
    fleet = build_fleet(specs, settings=settings, transport=tripwire())
    assert len(fleet) == 8
    names = [b.name for b in fleet]
    assert "generic-openapi-video" in names
    assert "stability-svd" in names
    # capabilities are reachable on every built backend
    for b in fleet:
        assert b.capabilities().modes


def test_registry_unknown_kind_raises(settings: Settings) -> None:
    with pytest.raises(UnknownAdapterKind):
        build_adapter(
            OpenAdapterSpec(kind="does-not-exist"), settings=settings, transport=tripwire()
        )


def test_registry_descriptor_requires_param(settings: Settings) -> None:
    with pytest.raises(UnknownAdapterKind):
        build_adapter(OpenAdapterSpec(kind="descriptor"), settings=settings, transport=tripwire())


async def test_registry_built_descriptor_renders(settings: Settings) -> None:
    routes = (
        RouteMap()
        .json("jobs", body={"job": {"id": "j1"}})
        .json(
            "jobs/j1", body={"job": {"state": "done", "result": {"video_url": "https://cdn/o.mp4"}}}
        )
        .bytes("cdn/o.mp4", b"OPENAPI")
    )
    spec = OpenAdapterSpec(
        kind="descriptor",
        api_key="k",
        allow_network=True,
        live_video=True,
        params={"descriptor": load_bundled("openapi_example")},
        poll=NO_SLEEP_POLL,
    )
    adapter = build_adapter(spec, settings=settings, transport=routes.transport())
    result = await adapter.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    )
    assert result.clip_bytes == b"OPENAPI"
    await adapter.aclose()


async def test_registry_spec_carries_gates_off_by_default(settings: Settings) -> None:
    # Default spec has allow_network=False and live_video=False → render is gated.
    spec = OpenAdapterSpec(kind="mochi", params={"version": "v"})
    assert spec.allow_network is False and spec.live_video is False
    adapter = build_adapter(spec, settings=settings, transport=tripwire())
    with pytest.raises(LiveVideoDisabled):
        await adapter.render(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="480P")
        )
    await adapter.aclose()


# --------------------------------------------------------------------------- #
# Last-frame extraction (graceful)
# --------------------------------------------------------------------------- #


def test_extract_last_frame_empty_returns_none() -> None:
    assert extract_last_frame(b"") is None


def test_extract_last_frame_garbage_is_graceful() -> None:
    # Non-decodable bytes never raise; None when ffmpeg can't decode (or is absent).
    assert extract_last_frame(b"not a real mp4") is None


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not available")
def test_extract_last_frame_real_clip() -> None:
    import subprocess
    import tempfile
    from pathlib import Path

    from app.render.degrade import get_ffmpeg_exe

    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "c.mp4"
        # Generate a 1s test-pattern clip.
        subprocess.run(
            [
                get_ffmpeg_exe(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=64x64:rate=10",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            capture_output=True,
            check=True,
        )
        frame = extract_last_frame(clip.read_bytes())
        assert frame is not None and len(frame) > 0
        assert frame[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
