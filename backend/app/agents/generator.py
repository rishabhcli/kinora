"""Generator — the real Wan + CosyVoice render bridge (§7, §9.2, §9.4).

This is NOT an LLM agent: it is the thin, real component that turns a designed
:class:`~app.agents.contracts.ShotSpec` into pixels and narration by calling the
providers. It maps the §9.3 ``render_mode`` to a :class:`WanSpec` and calls
``providers.video.render``; it synthesizes narration with
``providers.tts.synthesize`` and returns the word-timing map that drives karaoke
+ page-turn (§9.4).

Real Wan renders burn scarce, metered video-seconds, so ``video.render`` raises
:class:`LiveVideoDisabled` whenever ``KINORA_LIVE_VIDEO`` is off — and this
component **propagates** it rather than fabricating a clip. Degradation
(Ken-Burns over a keyframe) is the Phase-7 render pipeline's job, which also adds
the sync-map build, the Critic loop, stitching, and the degradation ladder
around this bridge.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.providers import Providers, WanMode, WanSpec, data_uri
from app.providers.types import TtsWord

from .contracts import RenderMode, ShotSpec

#: 1:1 value mapping from the agents' RenderMode to the provider's WanMode.
_RENDER_TO_WAN = {
    RenderMode.TEXT_TO_VIDEO: WanMode.TEXT_TO_VIDEO,
    RenderMode.IMAGE_TO_VIDEO: WanMode.IMAGE_TO_VIDEO,
    RenderMode.REFERENCE_TO_VIDEO: WanMode.REFERENCE_TO_VIDEO,
    RenderMode.FIRST_LAST_FRAME: WanMode.FIRST_LAST_FRAME,
    RenderMode.VIDEO_CONTINUATION: WanMode.VIDEO_CONTINUATION,
    RenderMode.INSTRUCTION_EDIT: WanMode.INSTRUCTION_EDIT,
}


class GeneratorOutput(BaseModel):
    """A rendered shot: the clip, its last frame, and the synced narration (§9.4)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    clip_bytes: bytes | None = None
    clip_url: str | None = None
    last_frame_bytes: bytes | None = None
    duration_s: float = 0.0
    audio_bytes: bytes = b""
    sample_rate: int = 0
    word_timestamps: list[TtsWord] = Field(default_factory=list)
    provider_task_id: str | None = None


def wan_mode_for(render_mode: RenderMode) -> WanMode:
    """Map a §9.3 render mode to the provider's Wan mode (by value)."""
    return _RENDER_TO_WAN[render_mode]


def _image_mime(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _as_data_uri(raw: bytes) -> str:
    return data_uri(raw, _image_mime(raw))


def build_wan_spec(
    spec: ShotSpec,
    *,
    reference_image_bytes: list[bytes] | None = None,
    prev_last_frame_bytes: bytes | None = None,
) -> WanSpec:
    """Translate a designed shot into a provider :class:`WanSpec` (pure).

    Image inputs are passed inline as ``data:`` URIs (the real way the providers
    accept image bytes); the Phase-7 pipeline may instead pass persisted OSS URLs.
    """
    mode = wan_mode_for(spec.render_mode)
    ref_urls = [_as_data_uri(b) for b in (reference_image_bytes or [])]
    prev_url = _as_data_uri(prev_last_frame_bytes) if prev_last_frame_bytes else None

    wan = WanSpec(
        mode=mode,
        prompt=spec.prompt,
        negative_prompt=spec.negative_prompt,
        seed=spec.seed,
        duration_s=int(round(spec.target_duration_s)),
        shot_id=spec.shot_id,
    )
    if mode is WanMode.REFERENCE_TO_VIDEO:
        wan.reference_image_urls = ref_urls
    elif mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
        wan.image_url = prev_url or (ref_urls[0] if ref_urls else None)
    elif mode is WanMode.FIRST_LAST_FRAME:
        wan.first_frame_url = ref_urls[0] if ref_urls else prev_url
        wan.last_frame_url = prev_url or spec.end_frame_ref
    elif mode is WanMode.INSTRUCTION_EDIT:
        wan.image_url = prev_url
    # TEXT_TO_VIDEO needs no image inputs.
    return wan


class Generator:
    """Renders a shot's clip + narration via the real providers (gated by budget)."""

    def __init__(self, providers: Providers) -> None:
        self._providers = providers

    async def render(
        self,
        spec: ShotSpec,
        *,
        narration_text: str,
        voice_id: str,
        reference_image_bytes: list[bytes] | None = None,
        prev_last_frame_bytes: bytes | None = None,
    ) -> GeneratorOutput:
        """Render the clip and narration for ``spec``.

        Video is rendered first so the ``LiveVideoDisabled`` gate short-circuits
        before any narration spend; on the live path, narration is then
        synthesized and its word-timing map returned for the sync map (§9.4).

        Raises:
            LiveVideoDisabled: when ``KINORA_LIVE_VIDEO`` is off (propagated from
                the video provider — never faked here).
        """
        wan_spec = build_wan_spec(
            spec,
            reference_image_bytes=reference_image_bytes,
            prev_last_frame_bytes=prev_last_frame_bytes,
        )
        video = await self._providers.video.render(wan_spec)
        narration = await self._providers.tts.synthesize(narration_text, voice_id=voice_id)
        return GeneratorOutput(
            clip_bytes=video.clip_bytes,
            clip_url=video.clip_url,
            last_frame_bytes=video.last_frame_bytes,
            duration_s=video.duration_s,
            audio_bytes=narration.audio_bytes,
            sample_rate=narration.sample_rate,
            word_timestamps=narration.word_timestamps,
            provider_task_id=video.provider_task_id,
        )


__all__ = ["Generator", "GeneratorOutput", "build_wan_spec", "wan_mode_for"]
