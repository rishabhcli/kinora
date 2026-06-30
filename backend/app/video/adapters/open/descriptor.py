"""The declarative **provider descriptor** — config-only onboarding of any model.

This is the headline deliverable of the open-adapter package: a JSON/YAML document
that fully describes how to drive an *arbitrary* self-hosted or gateway video model
— its capability profile, how to shape the submission body, where to POST it, how
to poll, and where the video URL lives in the response — so a brand-new model is
supported **with zero new Python**.

A descriptor has four parts:

1. ``capabilities`` — a :class:`~.interface.Capabilities` profile (modes,
   durations, resolutions, cost/quality) used for planning and the pre-submit
   validation.
2. ``transport`` — base URL, auth scheme, static headers (the network gate is
   supplied at *build* time, never baked into the file).
3. ``submit`` — the request ``path`` (``{{model}}`` interpolated) + a
   ``body_template`` of :mod:`.template` placeholders, plus the response selectors
   (:mod:`.jsonpath`) that locate the ``task_id`` and an optional inline
   ``poll_url``.
4. ``poll`` — the status ``path`` (``{{task_id}}`` interpolated) + selectors for
   the status string, the terminal vocabulary (which raw statuses mean
   succeeded / failed), the video-url selector, an optional inline-bytes (base64)
   selector, a progress selector, and a message selector.

:class:`ProviderDescriptor` is a pydantic v2 model, so a malformed file fails
loudly with a precise validation error. :func:`load_descriptor` reads JSON or YAML
from a path or string.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.providers.types import WanMode

from .interface import Capabilities

__all__ = [
    "CapabilitiesSpec",
    "PollSpec",
    "ProviderDescriptor",
    "SubmitSpec",
    "TransportSpec",
    "load_descriptor",
]


class CapabilitiesSpec(BaseModel):
    """The serialisable form of :class:`~.interface.Capabilities`."""

    model_config = ConfigDict(extra="forbid")

    modes: list[WanMode] = Field(default_factory=lambda: [WanMode.TEXT_TO_VIDEO])
    max_duration_s: float = 5.0
    min_duration_s: float = 1.0
    resolutions: list[str] = Field(default_factory=lambda: ["720P"])
    supports_seed: bool = True
    supports_negative_prompt: bool = False
    supports_audio: bool = False
    max_reference_images: int = 0
    default_fps: int = 24
    cost_per_s: float = 0.0
    quality: float = 0.5
    self_hosted: bool = False

    def to_capabilities(self, name: str) -> Capabilities:
        return Capabilities(
            name=name,
            modes=frozenset(self.modes),
            max_duration_s=self.max_duration_s,
            min_duration_s=self.min_duration_s,
            resolutions=frozenset(self.resolutions),
            supports_seed=self.supports_seed,
            supports_negative_prompt=self.supports_negative_prompt,
            supports_audio=self.supports_audio,
            max_reference_images=self.max_reference_images,
            default_fps=self.default_fps,
            cost_per_s=self.cost_per_s,
            quality=self.quality,
            self_hosted=self.self_hosted,
        )


class TransportSpec(BaseModel):
    """Connection shape for a descriptor (the network gate is supplied at build)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    auth_scheme: str = "bearer"  # bearer | token | key | none
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 60.0

    @field_validator("auth_scheme")
    @classmethod
    def _known_scheme(cls, v: str) -> str:
        if v not in ("bearer", "token", "key", "none"):
            raise ValueError(f"unknown auth_scheme {v!r}")
        return v


class SubmitSpec(BaseModel):
    """How to submit a render and where the task handle lives in the response."""

    model_config = ConfigDict(extra="forbid")

    #: Endpoint path (or absolute URL); ``{{model}}`` and any context var allowed.
    path: str
    method: str = "POST"
    #: The native request body, a :mod:`.template` of ``{{placeholders}}``.
    body_template: dict[str, Any] = Field(default_factory=dict)
    #: JSONPath to the task id in the submission response.
    task_id_path: str = "id || task_id || output.task_id || request_id"
    #: Optional JSONPath to a fully-formed poll URL the provider returns.
    poll_url_path: str | None = None


class PollSpec(BaseModel):
    """How to poll a task's status and extract the finished video."""

    model_config = ConfigDict(extra="forbid")

    #: Status endpoint path (or absolute URL); ``{{task_id}}`` interpolated.
    path: str
    method: str = "GET"
    #: JSONPath to the raw status string.
    status_path: str = "status || output.task_status || state"
    #: Raw status values (case-insensitive) that mean the task finished OK.
    succeeded_values: list[str] = Field(
        default_factory=lambda: ["succeeded", "success", "completed", "complete"]
    )
    #: Raw status values that mean the task failed terminally.
    failed_values: list[str] = Field(
        default_factory=lambda: ["failed", "error", "canceled", "cancelled"]
    )
    #: JSONPath to the result video URL (a fallback chain covers shape variants).
    video_url_path: str = (
        "output || output[0] || output.video_url || output.url || "
        "result.url || video.url || urls.get"
    )
    #: Optional JSONPath to base64-encoded inline clip bytes.
    inline_b64_path: str | None = None
    #: Optional JSONPath to a 0..1 (or 0..100) progress value.
    progress_path: str | None = None
    #: Optional JSONPath to a human-readable status/error message.
    message_path: str = "error || message || output.message || detail"


class ProviderDescriptor(BaseModel):
    """A complete, config-only description of one video model endpoint.

    Build a live adapter from it with
    :meth:`app.video.adapters.open.descriptor_adapter.DescriptorAdapter.from_descriptor`.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable provider/adapter id (routing + telemetry).
    name: str
    #: Native model id sent in the request (``{{model}}`` resolves to this unless a
    #: per-spec ``WanSpec.model`` override is present).
    model: str
    #: Schema/format version for forward-compatibility.
    version: int = 1
    capabilities: CapabilitiesSpec = Field(default_factory=CapabilitiesSpec)
    transport: TransportSpec
    submit: SubmitSpec
    poll: PollSpec

    def to_capabilities(self) -> Capabilities:
        return self.capabilities.to_capabilities(self.name)


def _decode_inline_b64(value: Any) -> bytes | None:
    """Decode a base64 string (data-URI tolerant) into bytes, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def load_descriptor(source: str | Path | dict[str, Any]) -> ProviderDescriptor:
    """Load a :class:`ProviderDescriptor` from a dict, a JSON/YAML string, or a path.

    YAML is parsed when ``pyyaml`` is importable; otherwise JSON is required (and a
    ``.yaml`` path without pyyaml raises a clear error). A ``dict`` is validated
    directly. The returned model is fully validated — a missing required key or an
    unknown field fails with a pydantic ``ValidationError``.
    """
    if isinstance(source, dict):
        return ProviderDescriptor.model_validate(source)

    if isinstance(source, Path) or (
        isinstance(source, str) and "\n" not in source and Path(source).exists()
    ):
        path = Path(source)
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
    else:
        text = str(source)
        suffix = ""

    data = _parse_structured(text, suffix)
    # Drop top-level ``_``-prefixed keys so a descriptor file may carry a
    # ``_comment`` (JSON has no comment syntax) without tripping ``extra=forbid``.
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return ProviderDescriptor.model_validate(data)


def _parse_structured(text: str, suffix: str) -> dict[str, Any]:
    """Parse ``text`` as YAML (preferred) or JSON into a mapping."""
    stripped = text.lstrip()
    if suffix in (".yaml", ".yml") or not stripped.startswith(("{", "[")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - pyyaml is a test/runtime dep
            if suffix in (".yaml", ".yml"):
                raise ValueError("YAML descriptor requires pyyaml; install it or use JSON") from exc
            return json.loads(text)
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError("descriptor must be a mapping at the top level")
        return loaded
    return json.loads(text)
