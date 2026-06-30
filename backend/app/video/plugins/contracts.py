"""The stable plugin contract — the surface a third-party video model implements.

This module is the *public ABI* of the SDK: a third-party author programs
against the types here and nothing else, so it must stay backward-compatible
within a major plugin-API version. It deliberately re-declares small, local
data types (``VideoRequest`` / ``VideoArtifact`` / ``CapabilityProfile``) rather
than importing Kinora's internal ``app.providers`` models, for two reasons:

1. **Final-round isolation.** The universal-provider contract from the earlier
   rounds is *not merged*; per the marathon rules this SDK mirrors that contract
   with a minimal LOCAL Protocol (:class:`VideoProviderPlugin`). The orchestrator
   wires the real universal provider to this protocol at final integration via a
   thin adapter — the field names here intentionally line up with
   ``app.providers.types.WanSpec`` / ``VideoResult`` so that adapter is trivial.
2. **A narrow trust boundary.** A plugin should depend on a tiny, frozen schema,
   not on the host's evolving internals; that keeps the conformance contract
   meaningful and the sandbox's import-denylist able to bar ``app`` entirely.

The contract has three parts:

* :class:`VideoProviderPlugin` — the Protocol every plugin's entry-point object
  satisfies (``capabilities`` / async ``probe`` / async ``generate``).
* :class:`VideoRequest` / :class:`VideoArtifact` — the request + result schema
  the plugin renders against (validated pydantic v2 models).
* :class:`CapabilityProfile` — the declarative description of *what a model can
  do* (modes, resolutions, duration bounds, conditioning inputs) that the host
  uses for routing and the conformance harness uses to pick which cases apply.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# The plugin-API version this SDK build implements
# --------------------------------------------------------------------------- #

#: The host plugin-API version. A plugin manifest declares the range of API
#: versions it supports via ``kinora_api``; discovery skips any plugin whose
#: range excludes this value, so a host upgrade that breaks the ABI surfaces as
#: a clean skip rather than a runtime crash. Bump the MAJOR when a
#: backward-incompatible change is made to anything in this module.
PLUGIN_API_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Render modes + the capability profile
# --------------------------------------------------------------------------- #


class RenderMode(StrEnum):
    """A generation mode a video model may support.

    Mirrors the §9.3 hosted-Wan decision-tree modes so the universal-provider
    adapter is a 1:1 map, but is re-declared locally to keep the plugin ABI free
    of host imports. A plugin advertises the subset it supports in its
    :class:`CapabilityProfile`; the host never asks a plugin for a mode it did
    not advertise.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


class CapabilityProfile(BaseModel):
    """A declarative description of what one video model can do.

    Pure data: used for routing (does this model support i2v at 1080P?) and to
    drive the conformance harness (which cases are *applicable* to this plugin).
    A plugin's manifest embeds one of these; the plugin object also exposes it at
    runtime via :attr:`VideoProviderPlugin.capabilities` and the two must agree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The render modes this model supports (must be non-empty).
    modes: frozenset[RenderMode]
    #: Advertised output resolutions (free-form tokens like ``"720P"``/``"1080P"``).
    resolutions: frozenset[str] = frozenset({"720P"})
    #: Inclusive clip-duration bounds in seconds.
    min_duration_s: float = 1.0
    max_duration_s: float = 10.0
    #: Whether the model accepts a negative prompt / seed for reproducibility.
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    #: Whether the model can emit word/frame timing or an audio track itself.
    supports_audio: bool = False
    #: Max reference images for reference_to_video (0 ⇒ r2v unsupported regardless).
    max_reference_images: int = 0

    @field_validator("modes")
    @classmethod
    def _modes_non_empty(cls, v: frozenset[RenderMode]) -> frozenset[RenderMode]:
        if not v:
            raise ValueError("a capability profile must advertise at least one mode")
        return v

    def supports(self, mode: RenderMode) -> bool:
        """True when ``mode`` is advertised by this profile."""
        return mode in self.modes

    def accepts_duration(self, seconds: float) -> bool:
        """True when ``seconds`` falls within the advertised duration bounds."""
        return self.min_duration_s <= seconds <= self.max_duration_s


# --------------------------------------------------------------------------- #
# Request / result schema the plugin renders against
# --------------------------------------------------------------------------- #


class VideoRequest(BaseModel):
    """A fully-resolved request for one clip, handed to a plugin's ``generate``.

    Field names mirror :class:`app.providers.types.WanSpec` so the final
    integration adapter is a field copy. Conditioning media are URLs (the host
    persists locked references / endpoint frames to object storage and passes
    signed URLs), never raw bytes — a sandboxed plugin must not touch the
    filesystem or the host's storage layer directly.
    """

    model_config = ConfigDict(extra="forbid")

    mode: RenderMode
    prompt: str = ""
    negative_prompt: str | None = None
    reference_image_urls: list[str] = Field(default_factory=list)
    reference_voice_url: str | None = None
    image_url: str | None = None
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    source_video_url: str | None = None
    seed: int | None = None
    duration_s: float = 5.0
    resolution: str = "720P"
    #: Opaque correlation id carried end-to-end for telemetry/idempotency.
    request_id: str | None = None


class VideoArtifact(BaseModel):
    """The result a plugin returns for one rendered clip.

    A plugin returns a *reference* to a rendered clip (a provider task id + a URL
    the host downloads), plus the realized duration and the model id that
    produced it. The host owns persistence (provider URLs expire); the plugin
    never writes to storage itself. Mirrors
    :class:`app.providers.types.VideoResult`.
    """

    model_config = ConfigDict(extra="forbid")

    clip_url: str
    duration_s: float
    model: str
    mode: RenderMode
    provider_task_id: str | None = None
    #: Whether the artifact includes an embedded audio track (advisory).
    has_audio: bool = False


class ProbeResult(BaseModel):
    """The outcome of a plugin's cheap, no-spend liveness/credentials probe."""

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    #: Optional human-readable detail (e.g. why a probe failed). Never a secret.
    detail: str = ""


# --------------------------------------------------------------------------- #
# The plugin Protocol — the ABI a plugin entry-point object satisfies
# --------------------------------------------------------------------------- #


@runtime_checkable
class VideoProviderPlugin(Protocol):
    """The contract a third-party video model implements.

    A plugin's entry point is a *factory* (see
    :class:`~app.video.plugins.manifest.PluginManifest.entry_point`) that the SDK
    calls with the plugin's validated config and a capability-scoped host handle,
    returning an object satisfying this Protocol. The object is then driven
    exclusively through these three members:

    * :attr:`capabilities` — the same :class:`CapabilityProfile` the manifest
      declared (used for routing + conformance-case selection);
    * :meth:`probe` — a cheap, no-render liveness/credentials check; and
    * :meth:`generate` — render one :class:`VideoRequest` to a
      :class:`VideoArtifact`.

    This mirrors the universal-provider contract from the earlier rounds with the
    minimal local surface the SDK needs; the orchestrator adapts the real
    universal provider onto it at final integration.
    """

    #: The model's advertised capabilities (must equal the manifest's profile).
    capabilities: CapabilityProfile

    async def probe(self) -> ProbeResult:
        """Cheap liveness/credentials check — never renders, never spends."""
        ...

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        """Render one clip. May raise the plugin's own errors (the host contains them)."""
        ...


#: The factory signature an entry point must expose. It receives the validated
#: config mapping and a capability-scoped host handle (typed ``object`` here so
#: the contract module stays free of the sandbox import — the real type is
#: :class:`app.video.plugins.sandbox.HostHandle`).
class PluginFactory(Protocol):
    def __call__(self, *, config: dict[str, object], host: object) -> VideoProviderPlugin:
        ...


__all__ = [
    "PLUGIN_API_VERSION",
    "CapabilityProfile",
    "PluginFactory",
    "ProbeResult",
    "RenderMode",
    "VideoArtifact",
    "VideoProviderPlugin",
    "VideoRequest",
]
