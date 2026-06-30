"""Unit tests for the canonical request/result schema + media refs.

Covers: MediaRef exactly-one-source guard, request role accessors, the
idempotency-key digest (stability + sensitivity), TaskState terminal logic, and
result clip-presence validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.video.abstraction.capability import VideoMode
from app.video.abstraction.schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    MediaRef,
    MediaRole,
    TaskState,
    VideoTaskHandle,
)

# -- MediaRef ------------------------------------------------------------- #


def test_media_ref_url_path() -> None:
    m = MediaRef(role=MediaRole.FIRST_FRAME, url="https://x/y.png")
    assert not m.is_inline


def test_media_ref_bytes_path() -> None:
    m = MediaRef(role=MediaRole.REFERENCE, data=b"\x89PNG", mime="image/png")
    assert m.is_inline


def test_media_ref_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        MediaRef(role=MediaRole.FIRST_FRAME)  # neither
    with pytest.raises(ValidationError):
        MediaRef(role=MediaRole.FIRST_FRAME, url="u", data=b"d")  # both


def test_media_ref_rejects_blank_url() -> None:
    with pytest.raises(ValidationError):
        MediaRef(role=MediaRole.FIRST_FRAME, url="   ")


# -- request accessors ---------------------------------------------------- #


def _req(**kw: object) -> CanonicalVideoRequest:
    base: dict[str, object] = {"mode": VideoMode.REFERENCE_TO_VIDEO, "prompt": "p"}
    base.update(kw)
    return CanonicalVideoRequest(**base)  # type: ignore[arg-type]


def test_media_role_accessors() -> None:
    req = _req(
        media=(
            MediaRef(role=MediaRole.REFERENCE, url="r1"),
            MediaRef(role=MediaRole.REFERENCE, url="r2"),
            MediaRef(role=MediaRole.REFERENCE_VOICE, url="v"),
        )
    )
    refs = req.references
    assert [m.url for m in refs] == ["r1", "r2"]
    assert req.first_media(MediaRole.REFERENCE_VOICE).url == "v"  # type: ignore[union-attr]
    assert req.first_media(MediaRole.LAST_FRAME) is None


def test_request_is_frozen_and_strict() -> None:
    req = _req()
    with pytest.raises(ValidationError):
        req.prompt = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _req(bogus=1)
    with pytest.raises(ValidationError):
        _req(duration_s=0)  # gt=0


# -- idempotency key ------------------------------------------------------ #


def test_idempotency_key_is_stable() -> None:
    a = _req(seed=7, duration_s=5.0, media=(MediaRef(role=MediaRole.REFERENCE, url="r1"),))
    b = _req(seed=7, duration_s=5.0, media=(MediaRef(role=MediaRole.REFERENCE, url="r1"),))
    assert a.idempotency_key() == b.idempotency_key()
    assert len(a.idempotency_key()) == 64  # sha256 hexdigest


@pytest.mark.parametrize(
    "mutation",
    [
        {"prompt": "different"},
        {"negative_prompt": "blurry"},
        {"seed": 8},
        {"duration_s": 6.0},
        {"resolution": "1080P"},
        {"model": "other"},
        {"shot_id": "shot_2"},
    ],
)
def test_idempotency_key_sensitive_to_each_field(mutation: dict[str, object]) -> None:
    base_kw: dict[str, object] = {"seed": 7}
    base = _req(**base_kw)
    other = _req(**{**base_kw, **mutation})
    assert base.idempotency_key() != other.idempotency_key()


def test_idempotency_key_sensitive_to_media_url_and_bytes() -> None:
    base = _req(media=(MediaRef(role=MediaRole.REFERENCE, url="r1"),))
    url_changed = _req(media=(MediaRef(role=MediaRole.REFERENCE, url="r2"),))
    bytes_a = _req(media=(MediaRef(role=MediaRole.REFERENCE, data=b"A"),))
    bytes_b = _req(media=(MediaRef(role=MediaRole.REFERENCE, data=b"B"),))
    assert base.idempotency_key() != url_changed.idempotency_key()
    assert bytes_a.idempotency_key() != bytes_b.idempotency_key()
    # identical bytes → identical key
    assert (
        _req(media=(MediaRef(role=MediaRole.REFERENCE, data=b"A"),)).idempotency_key()
        == bytes_a.idempotency_key()
    )


def test_idempotency_key_sensitive_to_media_role() -> None:
    a = _req(media=(MediaRef(role=MediaRole.FIRST_FRAME, url="u"),))
    b = _req(media=(MediaRef(role=MediaRole.LAST_FRAME, url="u"),))
    assert a.idempotency_key() != b.idempotency_key()


# -- TaskState ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("state", "terminal"),
    [
        (TaskState.PENDING, False),
        (TaskState.RUNNING, False),
        (TaskState.SUCCEEDED, True),
        (TaskState.FAILED, True),
        (TaskState.CANCELED, True),
    ],
)
def test_task_state_terminal(state: TaskState, terminal: bool) -> None:
    assert state.is_terminal is terminal


# -- handle + result ------------------------------------------------------ #


def test_task_handle_requires_ids() -> None:
    with pytest.raises(ValidationError):
        VideoTaskHandle(provider_id="", task_id="t")
    with pytest.raises(ValidationError):
        VideoTaskHandle(provider_id="p", task_id="")


def test_result_requires_a_clip() -> None:
    with pytest.raises(ValidationError):
        CanonicalVideoResult(
            provider_id="p", mode=VideoMode.TEXT_TO_VIDEO, duration_s=5.0
        )
    ok = CanonicalVideoResult(
        provider_id="p", mode=VideoMode.TEXT_TO_VIDEO, duration_s=5.0, clip_bytes=b"x"
    )
    assert ok.clip_bytes == b"x"


def test_result_negative_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        CanonicalVideoResult(
            provider_id="p",
            mode=VideoMode.TEXT_TO_VIDEO,
            duration_s=-1.0,
            clip_url="u",
        )
