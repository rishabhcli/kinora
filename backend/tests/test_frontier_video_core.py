"""Core (provider-agnostic) tests for the frontier video subsystem:
the error taxonomy, capability profiles + validation, canonical request mapping,
the transport (gate, retries, error mapping, download), and the base adapter
lifecycle (gating, snapping, submit→poll→fetch→render). No network, no keys."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)
from app.providers.types import WanMode, WanSpec
from app.video.adapters.frontier import errors as ferr
from app.video.adapters.frontier import types as ftypes
from app.video.adapters.frontier.errors import (
    FrontierAuthError,
    FrontierError,
    FrontierErrorCode,
    FrontierRateLimited,
    FrontierUnsupportedCapability,
    build_error,
    code_for_status,
)
from app.video.adapters.frontier.transport import (
    FrontierTransport,
    FrontierTransportDisabled,
)
from app.video.adapters.frontier.types import (
    CapabilityProfile,
    FrontierRequest,
    VideoMode,
    from_wan_spec,
    mode_from_wan,
    mode_to_wan,
    validate_against_profile,
)

from .frontier_video_helpers import FAST_RETRY, no_sleep

# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


def test_frontier_errors_subclass_provider_taxonomy() -> None:
    # The router branches on these base types — they must be true subclasses.
    assert issubclass(FrontierAuthError, AuthenticationError)
    assert issubclass(FrontierError, ProviderError)
    assert issubclass(ferr.FrontierBadRequest, ProviderBadRequest)
    assert issubclass(ferr.FrontierUnsupportedCapability, ProviderBadRequest)
    assert issubclass(ferr.FrontierRateLimited, RateLimited)
    assert issubclass(ferr.FrontierTimeout, ProviderTimeout)
    assert issubclass(ferr.FrontierServerError, TransientProviderError)


def test_retryability_follows_canonical_code() -> None:
    assert build_error(FrontierErrorCode.RATE_LIMITED, "x").retryable is True
    assert build_error(FrontierErrorCode.SERVER_ERROR, "x").retryable is True
    assert build_error(FrontierErrorCode.TIMEOUT, "x").retryable is True
    # Hard request failures must NOT be retried (they fail identically).
    assert build_error(FrontierErrorCode.INVALID_REQUEST, "x").retryable is False
    assert build_error(FrontierErrorCode.QUOTA_EXHAUSTED, "x").retryable is False
    assert build_error(FrontierErrorCode.CONTENT_MODERATED, "x").retryable is False
    assert build_error(FrontierErrorCode.AUTH, "x").retryable is False
    assert build_error(FrontierErrorCode.JOB_FAILED, "x").retryable is False


def test_code_for_status_mapping() -> None:
    assert code_for_status(401) is FrontierErrorCode.AUTH
    assert code_for_status(403) is FrontierErrorCode.AUTH
    assert code_for_status(400) is FrontierErrorCode.INVALID_REQUEST
    assert code_for_status(422) is FrontierErrorCode.INVALID_REQUEST
    assert code_for_status(429) is FrontierErrorCode.RATE_LIMITED
    assert code_for_status(503) is FrontierErrorCode.SERVER_ERROR
    assert code_for_status(418) is FrontierErrorCode.UNKNOWN


def test_build_error_attaches_retry_after_on_rate_limit() -> None:
    err = build_error(FrontierErrorCode.RATE_LIMITED, "slow down", retry_after_s=12.0)
    assert isinstance(err, FrontierRateLimited)
    assert err.retry_after_s == 12.0


def test_frontier_error_carries_provider_and_code() -> None:
    err = build_error(
        FrontierErrorCode.AUTH, "nope", provider="runway", native_code="X1", status_code=403
    )
    assert err.provider == "runway"
    assert err.code == "X1"
    assert err.status_code == 403
    assert err.code_enum is FrontierErrorCode.AUTH
    assert "reason=auth" in str(err)


# --------------------------------------------------------------------------- #
# Modes + canonical request mapping
# --------------------------------------------------------------------------- #


def test_mode_round_trips_every_wan_mode() -> None:
    for wan in WanMode:
        canonical = mode_from_wan(wan)
        assert mode_to_wan(canonical) is wan


def test_from_wan_spec_maps_fields_and_normalises_resolution() -> None:
    spec = WanSpec(
        mode=WanMode.IMAGE_TO_VIDEO,
        prompt="she turns",
        negative_prompt="blurry",
        image_url="https://x/a.png",
        seed=7,
        duration_s=5,
        resolution="720P",
        shot_id="shot-1",
        reference_image_urls=["https://x/ref.png"],
    )
    req = from_wan_spec(spec)
    assert req.mode is VideoMode.IMAGE_TO_VIDEO
    assert req.prompt == "she turns"
    assert req.negative_prompt == "blurry"
    assert req.image_url == "https://x/a.png"
    assert req.seed == 7
    assert req.duration_s == 5.0
    assert req.resolution == "720p"  # normalised lowercase
    assert req.shot_id == "shot-1"
    assert req.reference_image_urls == ["https://x/ref.png"]


def test_primary_image_priority() -> None:
    req = FrontierRequest(
        mode=VideoMode.IMAGE_TO_VIDEO,
        image_url="img",
        first_frame_url="first",
        reference_image_urls=["ref"],
    )
    assert req.primary_image() == "img"
    assert (
        FrontierRequest(mode=VideoMode.IMAGE_TO_VIDEO, first_frame_url="first").primary_image()
        == "first"
    )
    assert (
        FrontierRequest(
            mode=VideoMode.REFERENCE_TO_VIDEO, reference_image_urls=["ref"]
        ).primary_image()
        == "ref"
    )


# --------------------------------------------------------------------------- #
# Capability profile + validation
# --------------------------------------------------------------------------- #


def _profile(**overrides: object) -> CapabilityProfile:
    base: dict[str, object] = {
        "provider": "p",
        "model": "m",
        "modes": frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
        "durations_s": (5.0, 10.0),
        "resolutions": ("720p", "1080p"),
        "aspect_ratios": ("16:9", "9:16"),
        "fps_options": (24,),
        "supports_seed": True,
        "supports_negative_prompt": True,
        "max_reference_images": 1,
        "max_prompt_chars": 100,
    }
    base.update(overrides)
    return CapabilityProfile(**base)


def test_nearest_duration_snaps_to_supported_set_and_prefers_shorter_on_tie() -> None:
    p = _profile(durations_s=(5.0, 10.0))
    assert p.nearest_duration(4.0) == 5.0
    assert p.nearest_duration(9.0) == 10.0
    assert p.nearest_duration(7.5) == 5.0  # tie → shorter (cheaper)
    assert p.nearest_duration(100.0) == 10.0


def test_validate_rejects_unsupported_mode() -> None:
    p = _profile(modes=frozenset({VideoMode.TEXT_TO_VIDEO}))
    req = FrontierRequest(mode=VideoMode.IMAGE_TO_VIDEO, image_url="x", resolution="720p")
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(req, p)


def test_validate_rejects_unsupported_resolution_and_aspect_and_fps() -> None:
    p = _profile()
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(mode=VideoMode.TEXT_TO_VIDEO, resolution="4k", aspect_ratio="16:9"),
            p,
        )
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(mode=VideoMode.TEXT_TO_VIDEO, resolution="720p", aspect_ratio="1:1"),
            p,
        )
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.TEXT_TO_VIDEO, resolution="720p", aspect_ratio="16:9", fps=30
            ),
            p,
        )


def test_validate_rejects_seed_and_negative_prompt_when_unsupported() -> None:
    p = _profile(supports_seed=False, supports_negative_prompt=False)
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.TEXT_TO_VIDEO, resolution="720p", aspect_ratio="16:9", seed=1
            ),
            p,
        )
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.TEXT_TO_VIDEO,
                resolution="720p",
                aspect_ratio="16:9",
                negative_prompt="x",
            ),
            p,
        )


def test_validate_rejects_overlong_prompt_and_too_many_references() -> None:
    p = _profile(max_prompt_chars=10, max_reference_images=1)
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.TEXT_TO_VIDEO,
                prompt="x" * 11,
                resolution="720p",
                aspect_ratio="16:9",
            ),
            p,
        )
    p2 = _profile(modes=frozenset({VideoMode.REFERENCE_TO_VIDEO}), max_reference_images=2)
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.REFERENCE_TO_VIDEO,
                reference_image_urls=["a", "b", "c"],
                resolution="720p",
                aspect_ratio="16:9",
            ),
            p2,
        )


def test_validate_rejects_image_for_text_only_provider() -> None:
    p = _profile(modes=frozenset({VideoMode.IMAGE_TO_VIDEO}), max_reference_images=0)
    with pytest.raises(FrontierUnsupportedCapability):
        validate_against_profile(
            FrontierRequest(
                mode=VideoMode.IMAGE_TO_VIDEO, image_url="x", resolution="720p", aspect_ratio="16:9"
            ),
            p,
        )


def test_supported_modes_summary() -> None:
    p = _profile(provider="p", model="m")
    out = ftypes.supported_modes_summary([p])
    assert out == {"p:m": ["image_to_video", "text_to_video"]}


# --------------------------------------------------------------------------- #
# Transport: gate, retries, error mapping, download
# --------------------------------------------------------------------------- #


def _tx(
    handler: Any, *, enabled: bool = True, api_key: str | None = "sk", **kw: Any
) -> FrontierTransport:
    return FrontierTransport(
        base_url="https://h.test/v1",
        api_key=api_key,
        provider="p",
        enabled=enabled,
        transport=httpx.MockTransport(handler),
        retry=FAST_RETRY,
        sleeper=no_sleep,
        **kw,
    )


async def test_transport_refuses_network_when_flag_off() -> None:
    def tripwire(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not call the network when disabled")

    tx = _tx(tripwire, enabled=False)
    with pytest.raises(FrontierTransportDisabled):
        await tx.request_json("POST", "submit", op="submit", json={})
    with pytest.raises(FrontierTransportDisabled):
        await tx.download("https://h.test/clip.mp4")
    await tx.aclose()


async def test_transport_configured_property() -> None:
    on = _tx(lambda r: httpx.Response(200, json={}), enabled=True, api_key="sk")
    off_flag = _tx(lambda r: httpx.Response(200, json={}), enabled=False, api_key="sk")
    no_key = _tx(lambda r: httpx.Response(200, json={}), enabled=True, api_key=None)
    assert on.configured is True
    assert off_flag.configured is False
    assert no_key.configured is False
    for t in (on, off_flag, no_key):
        await t.aclose()


async def test_transport_sets_bearer_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization", "")
        return httpx.Response(200, json={"ok": True})

    tx = _tx(handler, api_key="sk-123")
    await tx.request_json("POST", "submit", op="submit", json={})
    assert seen["auth"] == "Bearer sk-123"
    await tx.aclose()


async def test_transport_retries_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"message": "overloaded"})
        return httpx.Response(200, json={"id": "ok"})

    tx = _tx(handler)
    body = await tx.request_json("POST", "submit", op="submit", json={})
    assert body == {"id": "ok"}
    assert calls["n"] == 3
    await tx.aclose()


async def test_transport_does_not_retry_4xx() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad", "code": "X"}})

    tx = _tx(handler)
    with pytest.raises(ProviderBadRequest) as ei:
        await tx.request_json("POST", "submit", op="submit", json={})
    assert calls["n"] == 1  # single attempt, no retry
    assert ei.value.status_code == 400
    await tx.aclose()


async def test_transport_429_is_retryable_rate_limit() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "slow"})
        return httpx.Response(200, json={"id": "ok"})

    tx = _tx(handler)
    body = await tx.request_json("POST", "submit", op="submit", json={})
    assert body == {"id": "ok"}
    assert calls["n"] == 2
    await tx.aclose()


async def test_transport_bad_json_on_success_raises_bad_response() -> None:
    tx = _tx(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(ferr.FrontierBadResponse):
        await tx.request_json("GET", "poll", op="poll")
    await tx.aclose()


async def test_transport_download_returns_bytes() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"MP4BYTES")

    tx = _tx(handler)
    data = await tx.download("https://cdn.test/clip.mp4")
    assert data == b"MP4BYTES"
    await tx.aclose()


async def test_transport_retry_after_caps_backoff() -> None:
    # A RateLimited with retry_after honours the server hint (capped at backoff_max).
    err = build_error(FrontierErrorCode.RATE_LIMITED, "x", retry_after_s=999.0)
    tx = _tx(lambda r: httpx.Response(200, json={}))
    delay = tx._backoff(1, err)  # noqa: SLF001 - testing the backoff policy
    assert delay == FAST_RETRY.backoff_max_s  # 999 capped to max (0.0 here)
    await tx.aclose()
