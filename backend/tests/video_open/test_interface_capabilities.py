"""Capability profile semantics: supports() / reasons_unsupported() / select_backend."""

from __future__ import annotations

from app.providers.types import VideoResult, WanMode, WanSpec
from app.video.adapters.open.interface import (
    Capabilities,
    OpenVideoBackend,
    TaskStatus,
    select_backend,
)


def _caps() -> Capabilities:
    return Capabilities(
        name="cap-test",
        modes=frozenset({WanMode.TEXT_TO_VIDEO, WanMode.REFERENCE_TO_VIDEO}),
        max_duration_s=8.0,
        min_duration_s=2.0,
        resolutions=frozenset({"720P", "480P"}),
        max_reference_images=3,
    )


def test_supports_happy_path() -> None:
    caps = _caps()
    assert caps.supports(WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=5, resolution="720P"))


def test_supports_rejects_wrong_mode() -> None:
    caps = _caps()
    spec = WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="x", duration_s=5, resolution="720P")
    assert caps.supports(spec) is False
    assert any("mode" in r for r in caps.reasons_unsupported(spec))


def test_supports_rejects_out_of_window_duration() -> None:
    caps = _caps()
    too_long = WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=20, resolution="720P")
    too_short = WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=1, resolution="720P")
    assert caps.supports(too_long) is False
    assert caps.supports(too_short) is False
    assert any("max" in r for r in caps.reasons_unsupported(too_long))
    assert any("min" in r for r in caps.reasons_unsupported(too_short))


def test_supports_rejects_unknown_resolution() -> None:
    caps = _caps()
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=5, resolution="1080P")
    assert caps.supports(spec) is False
    assert any("resolution" in r for r in caps.reasons_unsupported(spec))


def test_reference_image_count_bounds() -> None:
    caps = _caps()
    none_refs = WanSpec(mode=WanMode.REFERENCE_TO_VIDEO, duration_s=5, resolution="720P")
    ok_refs = WanSpec(
        mode=WanMode.REFERENCE_TO_VIDEO,
        reference_image_urls=["a", "b"],
        duration_s=5,
        resolution="720P",
    )
    too_many = WanSpec(
        mode=WanMode.REFERENCE_TO_VIDEO,
        reference_image_urls=["a", "b", "c", "d"],
        duration_s=5,
        resolution="720P",
    )
    assert caps.supports(none_refs) is False
    assert caps.supports(ok_refs) is True
    assert caps.supports(too_many) is False


def test_empty_resolutions_accepts_any() -> None:
    caps = Capabilities(
        name="any-res", modes=frozenset({WanMode.TEXT_TO_VIDEO}), resolutions=frozenset()
    )
    assert caps.supports(WanSpec(mode=WanMode.TEXT_TO_VIDEO, resolution="weird", duration_s=3))


def test_reasons_empty_when_supported() -> None:
    caps = _caps()
    assert (
        caps.reasons_unsupported(
            WanSpec(mode=WanMode.TEXT_TO_VIDEO, duration_s=5, resolution="480P")
        )
        == []
    )


def test_select_backend_picks_first_capable() -> None:
    class _Stub:
        def __init__(self, name: str, caps: Capabilities) -> None:
            self.name = name
            self._c = caps

        def capabilities(self) -> Capabilities:
            return self._c

        async def render(self, spec: WanSpec) -> VideoResult:  # pragma: no cover - not called
            raise NotImplementedError

        async def healthy(self) -> bool:  # pragma: no cover
            return True

    only_t2v = _Stub("a", Capabilities(name="a", modes=frozenset({WanMode.TEXT_TO_VIDEO})))
    only_i2v = _Stub("b", Capabilities(name="b", modes=frozenset({WanMode.IMAGE_TO_VIDEO})))
    spec = WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="x", resolution="720P", duration_s=3)
    chosen = select_backend([only_t2v, only_i2v], spec)
    assert chosen is only_i2v
    # None capable → None
    assert (
        select_backend(
            [only_t2v], WanSpec(mode=WanMode.REFERENCE_TO_VIDEO, reference_image_urls=["x"])
        )
        is None
    )
    # Stub structurally satisfies the router-facing Protocol
    assert isinstance(only_t2v, OpenVideoBackend)


def test_task_status_helpers() -> None:
    pending = TaskStatus(state=TaskStatus.PENDING)
    ok = TaskStatus(state=TaskStatus.SUCCEEDED, video_url="u")
    bad = TaskStatus(state=TaskStatus.FAILED, message="boom")
    assert pending.is_terminal is False and pending.ok is False
    assert ok.is_terminal is True and ok.ok is True
    assert bad.is_terminal is True and bad.ok is False
