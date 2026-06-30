"""Content-addressed render keys — the heart of the clip dedup layer.

Rendering a clip is the single most expensive operation in Kinora (a hosted Wan /
MiniMax video round-trip costs real money — kinora.md §11.1). The §8.7 shot cache
already gives *idempotency*: re-reading a page never re-renders, because the
``shot_hash`` folds in ``book_id``/``beat_id``/canon-version. But that hash is
deliberately **book-scoped** — two different books that ask for the *exact same*
shot (same prompt, camera, seed, mode, references, provider, duration) compute
different ``shot_hash`` values and so pay twice.

This module derives a second, complementary key — the **content-addressed render
key** (``RenderKey``) — purely from the *rendering inputs themselves*, with no
book/beat identity. Two semantically-identical render requests therefore collide
**across books and across sessions**, so the clip is rendered once for the whole
fleet. The canonical inputs are exactly the ones that determine the pixels:

    prompt + negative_prompt + camera + seed + render_mode
        + reference identity digest + provider + model + duration + resolution

Normalisation is **explicit** so cosmetically-different-but-semantically-identical
requests collide:

* prompts are Unicode-NFC-normalised, lower-cased, and whitespace-collapsed;
* the camera block is reduced to its meaningful ``(move, speed, shot_size)`` triple
  (unknown extra keys are dropped, missing keys default);
* the reference set is hashed *order-independently* (a shot's identity is the
  *set* of locked references it used, not their order — mirrors §8.7);
* the duration is quantised to a fixed grid so ``5.0`` and ``5.0001`` collide;
* provider/model are case-folded.

The key is a SHA-256 digest, prefixed with a short ``schema`` tag so a future
normalisation change can bump the version without colliding with old keys.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bump when the *normalisation* changes in a way that should mint fresh keys
#: (so a redeploy doesn't serve a stale clip for a now-different normalisation).
RENDER_KEY_SCHEMA = "rk1"

#: Unit separator — cannot appear in textual inputs, so the join is unambiguous
#: (mirrors :mod:`app.db.hashing`).
_SEP = "\x1f"

#: Duration quantisation grid (seconds). Durations are snapped to this grid so
#: ``5.0`` and ``5.004`` collide but ``5.0`` and ``6.0`` do not. 0.5s is finer
#: than any provider's billing granularity, so it never merges distinct clips.
_DURATION_QUANTUM_S = 0.5

#: Collapse any run of Unicode whitespace to a single ASCII space.
_WS_RE = re.compile(r"\s+", re.UNICODE)

#: The camera fields that actually influence the render (the §7.1 camera block).
_CAMERA_FIELDS = ("move", "speed", "shot_size")
_CAMERA_DEFAULTS: dict[str, str] = {"move": "static", "speed": "medium", "shot_size": "medium"}


def normalize_text(text: str | None) -> str:
    """Canonicalise a prompt for hashing: NFC, casefold, whitespace-collapse, trim.

    Two prompts that differ only in casing, Unicode form, or runs of whitespace
    (tabs, newlines, doubled spaces) normalise identically and so collide.
    """
    if not text:
        return ""
    nfc = unicodedata.normalize("NFC", text)
    collapsed = _WS_RE.sub(" ", nfc).strip()
    return collapsed.casefold()


def normalize_camera(camera: Mapping[str, Any] | None) -> tuple[str, str, str]:
    """Reduce a camera block to its meaningful ``(move, speed, shot_size)`` triple.

    Unknown extra keys are dropped (they do not affect the render contract) and
    missing keys fall back to the §7.1 defaults, so ``{}`` and the full default
    block normalise identically. Values are casefolded + trimmed.
    """
    cam = dict(camera or {})

    def field(name: str) -> str:
        raw = cam.get(name, _CAMERA_DEFAULTS[name])
        value = str(raw).strip().casefold()
        return value or _CAMERA_DEFAULTS[name]

    return tuple(field(name) for name in _CAMERA_FIELDS)  # type: ignore[return-value]


def reference_identity_digest(reference_image_ids: Iterable[str]) -> str:
    """Order-independent SHA-256 digest of a reference set (``ref:`` prefixed).

    A shot's visual identity is the *set* of locked references it used, not their
    order (§8.7). Duplicates and surrounding whitespace are removed before
    hashing so ``["a", "a", " b "]`` and ``["b", "a"]`` collide. An empty set has
    a stable, distinct digest (so "no references" never collides with a real set).
    """
    cleaned = sorted({rid.strip() for rid in reference_image_ids if rid and rid.strip()})
    joined = _SEP.join(cleaned)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
    return f"ref:{digest}"


def quantize_duration(seconds: float) -> float:
    """Snap a duration to the fixed grid so float jitter never splits a clip."""
    if seconds <= 0:
        return 0.0
    steps = round(seconds / _DURATION_QUANTUM_S)
    return round(steps * _DURATION_QUANTUM_S, 6)


class RenderInputs(BaseModel):
    """The canonical, render-determining inputs for one clip.

    This is the *content* of a render: everything that decides the output pixels
    and nothing that doesn't (no ``book_id``/``beat_id``/``shot_id`` — those are
    identity, not content, and excluding them is exactly what enables cross-book
    reuse). Construct one from a fully-resolved shot spec via :meth:`from_spec`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = ""
    negative_prompt: str | None = None
    render_mode: str = "reference_to_video"
    seed: int = 0
    camera: dict[str, Any] | None = None
    reference_image_ids: tuple[str, ...] = ()
    provider: str = "dashscope"
    model: str = ""
    duration_s: float = 5.0
    resolution: str | None = None

    @field_validator("reference_image_ids", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(v) for v in value)

    @classmethod
    def from_spec(
        cls,
        spec: Any,
        *,
        provider: str,
        model: str = "",
        resolution: str | None = None,
        duration_s: float | None = None,
    ) -> RenderInputs:
        """Build inputs from a fully-resolved shot spec (duck-typed).

        Accepts anything exposing the §7.1 shot-spec attributes
        (``app.memory.interfaces.ShotSpec`` and the agents' ``ShotSpec`` both
        fit). ``render_mode`` may be a ``str`` or a ``StrEnum`` (``.value`` is
        used when present). ``duration_s`` defaults to the spec's
        ``target_duration_s``.
        """
        render_mode = getattr(spec, "render_mode", "reference_to_video")
        render_mode = getattr(render_mode, "value", render_mode)
        camera = getattr(spec, "camera", None)
        if camera is not None and hasattr(camera, "model_dump"):
            camera = camera.model_dump()
        dur = duration_s if duration_s is not None else getattr(spec, "target_duration_s", 5.0)
        return cls(
            prompt=getattr(spec, "prompt", "") or "",
            negative_prompt=getattr(spec, "negative_prompt", None),
            render_mode=str(render_mode),
            seed=int(getattr(spec, "seed", 0) or 0),
            camera=dict(camera) if isinstance(camera, Mapping) else None,
            reference_image_ids=tuple(getattr(spec, "reference_image_ids", ()) or ()),
            provider=provider,
            model=model,
            duration_s=float(dur),
            resolution=resolution,
        )

    def canonical(self) -> dict[str, Any]:
        """The fully-normalised dict that the key digest is taken over.

        Exposed (and stable) so a caller can log *why* two requests collided or
        differed without recomputing the digest.
        """
        move, speed, shot_size = normalize_camera(self.camera)
        return {
            "schema": RENDER_KEY_SCHEMA,
            "prompt": normalize_text(self.prompt),
            "negative_prompt": normalize_text(self.negative_prompt),
            "render_mode": self.render_mode.strip().casefold(),
            "seed": int(self.seed),
            "camera": {"move": move, "speed": speed, "shot_size": shot_size},
            "ref_digest": reference_identity_digest(self.reference_image_ids),
            "provider": self.provider.strip().casefold(),
            "model": self.model.strip().casefold(),
            "duration_s": quantize_duration(self.duration_s),
            "resolution": (self.resolution or "").strip().casefold(),
        }

    def key(self) -> RenderKey:
        """Derive the content-addressed :class:`RenderKey` for these inputs."""
        canon = self.canonical()
        blob = json.dumps(canon, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return RenderKey(version=RENDER_KEY_SCHEMA, digest=digest)


class RenderKey(BaseModel):
    """A content-addressed render key — stable, comparable, and serialisable.

    Equal keys mean "render these would produce the same clip". The string form
    is ``"<version>:<64-hex>"``; :meth:`short` is the 16-hex form used for object
    keys and log lines. ``version`` is the normalisation-schema tag so a future
    normalisation change mints fresh keys instead of colliding with old ones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = RENDER_KEY_SCHEMA
    digest: str = Field(min_length=8)

    @property
    def value(self) -> str:
        """The full ``"<version>:<digest>"`` string (the cache key)."""
        return f"{self.version}:{self.digest}"

    def short(self) -> str:
        """A 16-hex abbreviation — object-key / log-friendly, still collision-safe."""
        return self.digest[:16]

    def __str__(self) -> str:
        return self.value

    def __hash__(self) -> int:
        return hash(self.value)


def render_key(inputs: RenderInputs) -> RenderKey:
    """Functional shorthand for ``inputs.key()``."""
    return inputs.key()


__all__ = [
    "RENDER_KEY_SCHEMA",
    "RenderInputs",
    "RenderKey",
    "normalize_camera",
    "normalize_text",
    "quantize_duration",
    "reference_identity_digest",
    "render_key",
]
