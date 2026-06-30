"""The :class:`Normalizer` — lossless translation between Kinora's Wan types,
the canonical schema, and provider-native dicts.

Kinora's render seam is built around :class:`app.providers.types.WanSpec` /
:class:`app.providers.types.VideoResult`. The Universal Video Provider abstraction
adds a neutral :class:`~app.video.abstraction.schema.CanonicalVideoRequest` /
:class:`~app.video.abstraction.schema.CanonicalVideoResult`. This module is the
bridge:

* :meth:`Normalizer.from_wan_spec` / :meth:`Normalizer.to_wan_spec` round-trip a
  Wan request through canonical **losslessly** — every WanSpec field has a
  canonical home and back. This is what lets the existing Generator keep building
  ``WanSpec`` while a new universal provider consumes canonical.
* :meth:`Normalizer.to_native` / :meth:`Normalizer.from_native_result` translate
  canonical ↔ a provider-native JSON dict for the (legacy Wan / Wan-2.7-media)
  request shapes — the same two shapes
  :class:`app.providers.video.VideoProvider` speaks — so an adapter can reuse this
  instead of re-deriving the body.
* :meth:`Normalizer.to_video_result` / :meth:`Normalizer.from_video_result` map
  the result types both ways.

The §9.3 mode ↔ media-role wiring is the heart of the round-trip and is kept in
one place (:data:`_MODE_ROLE_FILLERS`) so Wan, canonical, and native never drift.
Pure functions; no I/O, no network, no settings reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.providers.types import VideoResult, WanMode, WanSpec

from .capability import ReferenceStyle, VideoMode
from .schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    MediaRef,
    MediaRole,
)

# Value-for-value mode mapping (the StrEnum values are identical by design).
_WAN_TO_CANONICAL: dict[WanMode, VideoMode] = {
    WanMode.TEXT_TO_VIDEO: VideoMode.TEXT_TO_VIDEO,
    WanMode.IMAGE_TO_VIDEO: VideoMode.IMAGE_TO_VIDEO,
    WanMode.REFERENCE_TO_VIDEO: VideoMode.REFERENCE_TO_VIDEO,
    WanMode.FIRST_LAST_FRAME: VideoMode.FIRST_LAST_FRAME,
    WanMode.VIDEO_CONTINUATION: VideoMode.VIDEO_CONTINUATION,
    WanMode.INSTRUCTION_EDIT: VideoMode.INSTRUCTION_EDIT,
}
_CANONICAL_TO_WAN: dict[VideoMode, WanMode] = {v: k for k, v in _WAN_TO_CANONICAL.items()}


def wan_mode_to_canonical(mode: WanMode) -> VideoMode:
    """Map a :class:`WanMode` to its canonical :class:`VideoMode`."""
    return _WAN_TO_CANONICAL[mode]


def canonical_mode_to_wan(mode: VideoMode) -> WanMode:
    """Map a canonical :class:`VideoMode` to its :class:`WanMode`."""
    return _CANONICAL_TO_WAN[mode]


class Normalizer:
    """Stateless translator between Wan, canonical, and native request shapes.

    Stateless — every method is effectively a pure function — but exposed as a
    class so the composition root can inject a single shared instance and so a
    future provider family with a different native dialect can subclass and
    override only :meth:`to_native` / :meth:`from_native_result`.
    """

    # -- WanSpec <-> canonical (LOSSLESS) -------------------------------- #

    def from_wan_spec(self, spec: WanSpec) -> CanonicalVideoRequest:
        """Translate a :class:`WanSpec` into a canonical request (lossless).

        Every WanSpec input URL becomes a typed :class:`MediaRef`, preserving
        order so :meth:`to_wan_spec` reconstructs the original. The §9.3 mode
        dictates which URL fields are meaningful, but this method copies *all*
        present URLs into roles so an unusual spec still round-trips.
        """
        media: list[MediaRef] = []
        for url in spec.reference_image_urls:
            media.append(MediaRef(role=MediaRole.REFERENCE, url=url))
        if spec.reference_voice_url:
            media.append(MediaRef(role=MediaRole.REFERENCE_VOICE, url=spec.reference_voice_url))
        if spec.image_url:
            media.append(MediaRef(role=MediaRole.FIRST_FRAME, url=spec.image_url))
        if spec.first_frame_url:
            media.append(MediaRef(role=MediaRole.FIRST_FRAME, url=spec.first_frame_url))
        if spec.last_frame_url:
            media.append(MediaRef(role=MediaRole.LAST_FRAME, url=spec.last_frame_url))
        if spec.source_video_url:
            media.append(MediaRef(role=MediaRole.SOURCE_VIDEO, url=spec.source_video_url))

        return CanonicalVideoRequest(
            mode=wan_mode_to_canonical(spec.mode),
            prompt=spec.prompt,
            negative_prompt=spec.negative_prompt,
            media=tuple(media),
            seed=spec.seed,
            duration_s=float(spec.duration_s),
            resolution=spec.resolution,
            watermark=spec.watermark,
            prompt_extend=spec.prompt_extend,
            model=spec.model,
            shot_id=spec.shot_id,
        )

    def to_wan_spec(self, request: CanonicalVideoRequest) -> WanSpec:
        """Translate a canonical request back into a :class:`WanSpec` (lossless).

        Inverse of :meth:`from_wan_spec`. The first ``FIRST_FRAME`` ref maps to
        ``image_url`` for i2v/continuation/edit; for first-last-frame it maps to
        ``first_frame_url`` so the disambiguation matches the §9.3 wiring. URL refs
        only — an inline-bytes ref cannot live in a WanSpec, which is URL-only by
        design (the pipeline persists bytes to OSS first); such a ref raises.
        """
        for m in request.media:
            if m.is_inline:
                raise ValueError(
                    "cannot map an inline-bytes MediaRef into a URL-only WanSpec; "
                    "persist it to object storage and pass a URL"
                )

        mode = canonical_mode_to_wan(request.mode)
        refs = [m.url for m in request.media_for(MediaRole.REFERENCE) if m.url]
        voice = request.first_media(MediaRole.REFERENCE_VOICE)
        first = request.first_media(MediaRole.FIRST_FRAME)
        last = request.first_media(MediaRole.LAST_FRAME)
        source = request.first_media(MediaRole.SOURCE_VIDEO)

        spec = WanSpec(
            mode=mode,
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            reference_image_urls=refs,
            reference_voice_url=voice.url if voice else None,
            seed=request.seed,
            duration_s=int(round(request.duration_s)),
            resolution=request.resolution or "720P",
            watermark=request.watermark,
            prompt_extend=request.prompt_extend,
            model=request.model,
            shot_id=request.shot_id,
        )
        # first-last-frame disambiguates a leading frame to first_frame_url; every
        # other single-frame mode uses image_url (matches §9.3 + the Wan provider).
        if first is not None:
            if mode is WanMode.FIRST_LAST_FRAME:
                spec.first_frame_url = first.url
            else:
                spec.image_url = first.url
        if last is not None:
            spec.last_frame_url = last.url
        if source is not None:
            spec.source_video_url = source.url
        return spec

    # -- canonical <-> native request dict ------------------------------- #

    def to_native(
        self,
        request: CanonicalVideoRequest,
        *,
        model: str,
        reference_style: ReferenceStyle = ReferenceStyle.TYPED_MEDIA,
    ) -> dict[str, Any]:
        """Render a canonical request into a provider-native JSON body.

        Produces the two shapes the hosted Wan provider speaks:

        * :data:`ReferenceStyle.TYPED_MEDIA` → ``input.media`` typed array (Wan 2.7).
        * :data:`ReferenceStyle.SINGLE_IMAGE` / ``MULTI_IMAGE`` → legacy single-image
          fields (``img_url`` / ``first_frame_url`` / ``video_url`` …).

        The returned dict mirrors DashScope's ``{"model", "input", "parameters"}``
        envelope so an adapter can submit it verbatim. ``provider_options`` keys
        are merged into ``parameters`` last (caller-supplied knobs win).
        """
        input_body: dict[str, Any] = {"prompt": request.prompt}
        if reference_style is ReferenceStyle.TYPED_MEDIA:
            media = self._typed_media(request)
            if media:
                input_body["media"] = media
        else:
            self._legacy_inputs(input_body, request)

        params: dict[str, Any] = {
            "duration": int(round(request.duration_s)),
            "watermark": request.watermark,
            "prompt_extend": request.prompt_extend,
        }
        if request.resolution:
            params["resolution"] = request.resolution
        if request.aspect_ratio:
            params["aspect_ratio"] = request.aspect_ratio
        if request.fps:
            params["fps"] = request.fps
        if request.negative_prompt:
            params["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            params["seed"] = request.seed
        params.update(request.provider_options)
        return {"model": model, "input": input_body, "parameters": params}

    @staticmethod
    def _typed_media(request: CanonicalVideoRequest) -> list[dict[str, str]]:
        """Build the Wan-2.7 ``input.media`` typed array from canonical media.

        Maps each canonical role to the native media ``type``; ``SOURCE_VIDEO``
        becomes ``first_clip`` (continuation / edit) and ``REFERENCE_VOICE`` is
        omitted (it rides ``parameters`` in the legacy lane and has no media slot
        in the typed array).
        """
        type_for = {
            MediaRole.FIRST_FRAME: "first_frame",
            MediaRole.LAST_FRAME: "last_frame",
            MediaRole.REFERENCE: "reference_image",
            MediaRole.SOURCE_VIDEO: "first_clip",
        }
        out: list[dict[str, str]] = []
        for m in request.media:
            native_type = type_for.get(m.role)
            if native_type is None or m.url is None:
                continue
            out.append({"type": native_type, "url": m.url})
        return out

    @staticmethod
    def _legacy_inputs(input_body: dict[str, Any], request: CanonicalVideoRequest) -> None:
        """Fill legacy single-image input fields from canonical media (Wan 2.1/2.2/2.5)."""
        first = request.first_media(MediaRole.FIRST_FRAME)
        last = request.first_media(MediaRole.LAST_FRAME)
        source = request.first_media(MediaRole.SOURCE_VIDEO)
        refs = [m for m in request.media_for(MediaRole.REFERENCE) if m.url]
        voice = request.first_media(MediaRole.REFERENCE_VOICE)

        mode = request.mode
        if mode is VideoMode.FIRST_LAST_FRAME:
            if first and first.url:
                input_body["first_frame_url"] = first.url
            if last and last.url:
                input_body["last_frame_url"] = last.url
            return
        if mode is VideoMode.REFERENCE_TO_VIDEO:
            if refs:
                input_body["img_url"] = refs[0].url
                if len(refs) > 1:
                    input_body["reference_image_urls"] = [m.url for m in refs]
            if voice and voice.url:
                input_body["reference_voice_url"] = voice.url
            return
        if mode in (VideoMode.IMAGE_TO_VIDEO, VideoMode.VIDEO_CONTINUATION):
            if first and first.url:
                input_body["img_url"] = first.url
            if source and source.url:
                input_body["video_url"] = source.url
            return
        if mode is VideoMode.INSTRUCTION_EDIT and source and source.url:
            input_body["video_url"] = source.url

    @staticmethod
    def from_native_result(
        node: Mapping[str, Any],
        *,
        provider_id: str,
        request: CanonicalVideoRequest,
        model: str,
        clip_url: str | None = None,
        clip_bytes: bytes | None = None,
        task_id: str | None = None,
    ) -> CanonicalVideoResult:
        """Build a canonical result from a native task-output dict + clip payload.

        ``node`` is the provider's ``output`` block (for resolution/fps/seed echo);
        the actual clip URL/bytes are passed explicitly since fetching them is the
        adapter's job, not the normalizer's.
        """
        resolution = _str_or_none(node.get("resolution")) or request.resolution
        fps = _int_or_none(node.get("fps")) or request.fps
        seed = _int_or_none(node.get("seed"))
        if seed is None:
            seed = request.seed
        return CanonicalVideoResult(
            provider_id=provider_id,
            mode=request.mode,
            model=model,
            duration_s=float(request.duration_s),
            clip_url=clip_url,
            clip_bytes=clip_bytes,
            resolution=resolution,
            fps=fps,
            provider_task_id=task_id,
            seed=seed,
        )

    # -- VideoResult <-> canonical result -------------------------------- #

    @staticmethod
    def to_video_result(result: CanonicalVideoResult) -> VideoResult:
        """Map a canonical result into Kinora's :class:`VideoResult`."""
        return VideoResult(
            duration_s=result.duration_s,
            model=result.model,
            mode=canonical_mode_to_wan(result.mode),
            provider_task_id=result.provider_task_id,
            clip_url=result.clip_url,
            clip_bytes=result.clip_bytes,
            last_frame_bytes=result.last_frame_bytes,
        )

    @staticmethod
    def from_video_result(result: VideoResult, *, provider_id: str) -> CanonicalVideoResult:
        """Map a Kinora :class:`VideoResult` into a canonical result."""
        return CanonicalVideoResult(
            provider_id=provider_id,
            mode=wan_mode_to_canonical(result.mode),
            model=result.model,
            duration_s=result.duration_s,
            clip_url=result.clip_url,
            clip_bytes=result.clip_bytes,
            last_frame_bytes=result.last_frame_bytes,
            provider_task_id=result.provider_task_id,
        )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


__all__ = [
    "Normalizer",
    "canonical_mode_to_wan",
    "wan_mode_to_canonical",
]
