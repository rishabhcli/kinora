"""Unit tests for the Normalizer: lossless WanSpec <-> canonical round-trips,
canonical -> native (typed-media + legacy) request bodies, and VideoResult maps.

The round-trip is the load-bearing guarantee: every §9.3 mode must survive
``WanSpec -> canonical -> WanSpec`` byte-for-byte on the fields the mode uses.
"""

from __future__ import annotations

import pytest

from app.providers.types import VideoResult, WanMode, WanSpec
from app.video.abstraction.capability import ReferenceStyle, VideoMode
from app.video.abstraction.normalizer import (
    Normalizer,
    canonical_mode_to_wan,
    wan_mode_to_canonical,
)
from app.video.abstraction.schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    MediaRef,
    MediaRole,
)

N = Normalizer()


# -- mode mapping --------------------------------------------------------- #


@pytest.mark.parametrize("mode", list(WanMode))
def test_mode_round_trip(mode: WanMode) -> None:
    assert canonical_mode_to_wan(wan_mode_to_canonical(mode)) is mode


# -- WanSpec -> canonical -> WanSpec, every mode -------------------------- #


def _specs() -> list[WanSpec]:
    return [
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a meadow", duration_s=5, seed=11),
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            prompt="she walks",
            image_url="https://oss/start.png",
            duration_s=5,
        ),
        WanSpec(
            mode=WanMode.REFERENCE_TO_VIDEO,
            prompt="hero speaks",
            reference_image_urls=["https://oss/ref1.png", "https://oss/ref2.png"],
            reference_voice_url="https://oss/voice.wav",
            negative_prompt="blurry, extra limbs",
            duration_s=5,
            seed=42,
        ),
        WanSpec(
            mode=WanMode.FIRST_LAST_FRAME,
            prompt="land on the doorway",
            first_frame_url="https://oss/first.png",
            last_frame_url="https://oss/last.png",
            duration_s=5,
        ),
        WanSpec(
            mode=WanMode.VIDEO_CONTINUATION,
            prompt="continue the chase",
            image_url="https://oss/endpoint.png",
            source_video_url="https://oss/prev.mp4",
            duration_s=5,
        ),
        WanSpec(
            mode=WanMode.INSTRUCTION_EDIT,
            prompt="make the coat red",
            source_video_url="https://oss/accepted.mp4",
            duration_s=5,
        ),
    ]


@pytest.mark.parametrize("spec", _specs(), ids=[s.mode.value for s in _specs()])
def test_wan_spec_round_trip_lossless(spec: WanSpec) -> None:
    """WanSpec -> canonical -> WanSpec preserves every field for the mode."""
    canonical = N.from_wan_spec(spec)
    back = N.to_wan_spec(canonical)
    assert back.mode is spec.mode
    assert back.prompt == spec.prompt
    assert back.negative_prompt == spec.negative_prompt
    assert back.reference_image_urls == spec.reference_image_urls
    assert back.reference_voice_url == spec.reference_voice_url
    assert back.image_url == spec.image_url
    assert back.first_frame_url == spec.first_frame_url
    assert back.last_frame_url == spec.last_frame_url
    assert back.source_video_url == spec.source_video_url
    assert back.seed == spec.seed
    assert back.duration_s == spec.duration_s
    assert back.resolution == spec.resolution


def test_canonical_carries_shot_id_and_extras() -> None:
    spec = WanSpec(
        mode=WanMode.TEXT_TO_VIDEO,
        prompt="x",
        shot_id="shot_00042",
        watermark=True,
        prompt_extend=True,
        model="wan2.7-t2v",
        resolution="1080P",
    )
    c = N.from_wan_spec(spec)
    assert c.shot_id == "shot_00042"
    assert c.watermark is True
    assert c.prompt_extend is True
    assert c.model == "wan2.7-t2v"
    assert c.resolution == "1080P"
    back = N.to_wan_spec(c)
    assert (back.shot_id, back.watermark, back.prompt_extend, back.model) == (
        "shot_00042",
        True,
        True,
        "wan2.7-t2v",
    )


def test_to_wan_spec_rejects_inline_bytes() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        media=(MediaRef(role=MediaRole.REFERENCE, data=b"PNG"),),
    )
    with pytest.raises(ValueError, match="inline-bytes"):
        N.to_wan_spec(req)


def test_first_frame_disambiguation_flf_vs_i2v() -> None:
    """A FIRST_FRAME ref maps to first_frame_url for FLF, image_url otherwise."""
    flf = CanonicalVideoRequest(
        mode=VideoMode.FIRST_LAST_FRAME,
        media=(MediaRef(role=MediaRole.FIRST_FRAME, url="f"),),
    )
    i2v = CanonicalVideoRequest(
        mode=VideoMode.IMAGE_TO_VIDEO,
        media=(MediaRef(role=MediaRole.FIRST_FRAME, url="f"),),
    )
    assert N.to_wan_spec(flf).first_frame_url == "f"
    assert N.to_wan_spec(flf).image_url is None
    assert N.to_wan_spec(i2v).image_url == "f"


# -- canonical -> native (typed media, Wan 2.7) --------------------------- #


def test_to_native_typed_media_r2v() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        prompt="hero",
        negative_prompt="blurry",
        seed=42,
        duration_s=5.0,
        resolution="720P",
        media=(
            MediaRef(role=MediaRole.REFERENCE, url="r1"),
            MediaRef(role=MediaRole.REFERENCE, url="r2"),
        ),
    )
    body = N.to_native(req, model="wan2.7-i2v", reference_style=ReferenceStyle.TYPED_MEDIA)
    assert body["model"] == "wan2.7-i2v"
    assert body["input"]["prompt"] == "hero"
    assert body["input"]["media"] == [
        {"type": "reference_image", "url": "r1"},
        {"type": "reference_image", "url": "r2"},
    ]
    assert body["parameters"]["seed"] == 42
    assert body["parameters"]["negative_prompt"] == "blurry"
    assert body["parameters"]["duration"] == 5
    assert body["parameters"]["resolution"] == "720P"


def test_to_native_typed_media_flf_and_continuation() -> None:
    flf = CanonicalVideoRequest(
        mode=VideoMode.FIRST_LAST_FRAME,
        media=(
            MediaRef(role=MediaRole.FIRST_FRAME, url="f"),
            MediaRef(role=MediaRole.LAST_FRAME, url="l"),
        ),
    )
    body = N.to_native(flf, model="wan2.7", reference_style=ReferenceStyle.TYPED_MEDIA)
    assert body["input"]["media"] == [
        {"type": "first_frame", "url": "f"},
        {"type": "last_frame", "url": "l"},
    ]
    cont = CanonicalVideoRequest(
        mode=VideoMode.VIDEO_CONTINUATION,
        media=(MediaRef(role=MediaRole.SOURCE_VIDEO, url="prev.mp4"),),
    )
    body2 = N.to_native(cont, model="wan2.7", reference_style=ReferenceStyle.TYPED_MEDIA)
    assert body2["input"]["media"] == [{"type": "first_clip", "url": "prev.mp4"}]


def test_to_native_t2v_has_no_media() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, prompt="vista")
    body = N.to_native(req, model="wan2.1-t2v-turbo")
    assert "media" not in body["input"]


# -- canonical -> native (legacy single-image, Wan 2.1/2.2/2.5) ----------- #


def test_to_native_legacy_i2v_uses_img_url() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.IMAGE_TO_VIDEO,
        media=(MediaRef(role=MediaRole.FIRST_FRAME, url="start.png"),),
    )
    body = N.to_native(req, model="wan2.1-i2v-turbo", reference_style=ReferenceStyle.SINGLE_IMAGE)
    assert body["input"]["img_url"] == "start.png"
    assert "media" not in body["input"]


def test_to_native_legacy_r2v_multi_ref_and_voice() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        media=(
            MediaRef(role=MediaRole.REFERENCE, url="r1"),
            MediaRef(role=MediaRole.REFERENCE, url="r2"),
            MediaRef(role=MediaRole.REFERENCE_VOICE, url="v"),
        ),
    )
    body = N.to_native(req, model="wan2.1-i2v-turbo", reference_style=ReferenceStyle.MULTI_IMAGE)
    assert body["input"]["img_url"] == "r1"
    assert body["input"]["reference_image_urls"] == ["r1", "r2"]
    assert body["input"]["reference_voice_url"] == "v"


def test_to_native_legacy_flf_and_edit() -> None:
    flf = CanonicalVideoRequest(
        mode=VideoMode.FIRST_LAST_FRAME,
        media=(
            MediaRef(role=MediaRole.FIRST_FRAME, url="f"),
            MediaRef(role=MediaRole.LAST_FRAME, url="l"),
        ),
    )
    body = N.to_native(flf, model="wan2.2", reference_style=ReferenceStyle.SINGLE_IMAGE)
    assert body["input"]["first_frame_url"] == "f"
    assert body["input"]["last_frame_url"] == "l"
    edit = CanonicalVideoRequest(
        mode=VideoMode.INSTRUCTION_EDIT,
        media=(MediaRef(role=MediaRole.SOURCE_VIDEO, url="clip.mp4"),),
    )
    body2 = N.to_native(edit, model="wan2.2", reference_style=ReferenceStyle.SINGLE_IMAGE)
    assert body2["input"]["video_url"] == "clip.mp4"


def test_to_native_merges_provider_options_last() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO,
        seed=1,
        provider_options={"seed": 999, "custom_knob": "x"},
    )
    body = N.to_native(req, model="m")
    # caller-supplied provider_options win the merge
    assert body["parameters"]["seed"] == 999
    assert body["parameters"]["custom_knob"] == "x"


def test_to_native_includes_aspect_and_fps_when_set() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO, aspect_ratio="9:16", fps=24
    )
    body = N.to_native(req, model="m")
    assert body["parameters"]["aspect_ratio"] == "9:16"
    assert body["parameters"]["fps"] == 24


# -- result mapping ------------------------------------------------------- #


def test_video_result_round_trip() -> None:
    canonical = CanonicalVideoResult(
        provider_id="p",
        mode=VideoMode.REFERENCE_TO_VIDEO,
        model="wan2.7-i2v",
        duration_s=5.0,
        clip_url="https://oss/clip.mp4",
        clip_bytes=b"MP4",
        last_frame_bytes=b"PNG",
        provider_task_id="task-1",
    )
    vr = N.to_video_result(canonical)
    assert isinstance(vr, VideoResult)
    assert vr.mode is WanMode.REFERENCE_TO_VIDEO
    assert vr.clip_url == "https://oss/clip.mp4"
    assert vr.clip_bytes == b"MP4"
    back = N.from_video_result(vr, provider_id="p")
    assert back.mode is VideoMode.REFERENCE_TO_VIDEO
    assert back.clip_bytes == b"MP4"
    assert back.last_frame_bytes == b"PNG"
    assert back.provider_task_id == "task-1"


def test_from_native_result_echoes_geometry_and_seed() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO, duration_s=5.0, resolution="720P", seed=7
    )
    out = N.from_native_result(
        {"resolution": "1080P", "fps": "30", "seed": 7},
        provider_id="p",
        request=req,
        model="m",
        clip_url="u",
        task_id="t",
    )
    assert out.resolution == "1080P"  # provider-reported wins over request
    assert out.fps == 30
    assert out.seed == 7
    assert out.clip_url == "u"
    assert out.duration_s == 5.0


def test_from_native_result_falls_back_to_request_geometry() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO, duration_s=5.0, resolution="720P", fps=24, seed=3
    )
    out = N.from_native_result(
        {}, provider_id="p", request=req, model="m", clip_bytes=b"x"
    )
    assert out.resolution == "720P"
    assert out.fps == 24
    assert out.seed == 3
