"""``RenderReplay`` — reconstruct the exact provider request from a fingerprint.

A fingerprint records *what* produced a clip; a replay turns that record back
into an actionable request. Two things make replay non-trivial:

1. **Signed URLs expire.** The reference-image and source-clip URLs captured at
   render time are short-lived (CLAUDE.md: "task URLs expire"). A faithful replay
   therefore cannot just reuse the recorded URL — it must *re-resolve* each
   reference identity (``char_elsa_001@v3``) to a fresh signed URL through a
   resolver seam. The locked reference *content* is immutable (§8.1), so a
   re-resolved URL points at the same bytes; that is what keeps the replay's
   ``request_digest`` identical to the original.
2. **The prompt/negative-prompt are not in the keyed digest.** They are recorded
   verbatim on the fingerprint (``prompt`` / ``negative_prompt``), so the replay
   reconstructs them losslessly rather than re-deriving them.

The replay returns a *reconstructed* :class:`WanSpec` plus a
:class:`ReplayPlan` that states, given the model's determinism label, whether
re-issuing will yield the **identical clip** (GUARANTEED), the **same plan**
(BEST_EFFORT), or merely the **same request, fresh roll** (NONE). It never issues
the call itself and never touches the live gate — the SDK rule is firm: no spend.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.providers.types import WanMode, WanSpec

from .classifier import ReproLabel
from .fingerprint import RenderFingerprint


class ReferenceResolver(Protocol):
    """Seam that re-resolves a stable reference identity to a *fresh* URL.

    Implementations live in the render/storage layer (object storage signs a new
    URL for the same immutable object). Kept as a Protocol so this module has no
    I/O dependency and tests can pass a pure dict-backed double.
    """

    def resolve_reference_url(self, reference_id: str) -> str:
        """Return a currently-valid signed URL for a ``id@version`` reference."""
        ...


class _IdentityResolver:
    """Default resolver: echo the recorded URLs by position.

    Used when no live resolver is supplied (e.g. an offline audit that only needs
    the reconstructed *shape*, not freshly-signed URLs). Maps each reference id to
    the URL recorded at the same index on the fingerprint, falling back to the id
    itself so the spec is never left with a dangling reference.
    """

    def __init__(self, fp: RenderFingerprint) -> None:
        self._by_id = dict(
            zip(fp.reference_image_ids, fp.reference_image_urls, strict=False)
        )

    def resolve_reference_url(self, reference_id: str) -> str:
        return self._by_id.get(reference_id, reference_id)


class ReplayPlan(BaseModel):
    """The outcome of reconstructing a request from a fingerprint."""

    model_config = ConfigDict(frozen=True)

    spec: WanSpec
    #: The provider/model the request must be re-issued to (recorded, immutable).
    provider: str
    model: str
    version: str | None = None
    #: Reproducibility promise of re-issuing this request (from the determinism
    #: label baked into the fingerprint).
    label: ReproLabel
    #: True when the reconstructed spec's keyed inputs match the original — i.e.
    #: re-issuing would re-produce the same ``request_digest``. A replay that has
    #: to substitute (e.g. an unresolvable reference) sets this False.
    faithful: bool
    #: Stable digest of the request the replay reconstructed; equals the original
    #: fingerprint's ``request_digest`` when ``faithful`` is True.
    reconstructed_request_digest: str
    #: Human notes (e.g. "re-resolved 2 reference URLs", "model is plan-stable").
    notes: tuple[str, ...] = ()

    def will_reproduce_bytes(self) -> bool:
        """Whether re-issuing is promised to return the identical clip bytes."""
        return self.faithful and self.label is ReproLabel.GUARANTEED

    def will_reproduce_plan(self) -> bool:
        """Whether re-issuing is promised to return the same composition."""
        return self.faithful and self.label in (
            ReproLabel.GUARANTEED,
            ReproLabel.BEST_EFFORT,
        )


class RenderReplay:
    """Reconstruct provider requests from fingerprints (no I/O, no spend)."""

    def __init__(self, resolver: ReferenceResolver | None = None) -> None:
        self._resolver = resolver

    def reconstruct(self, fp: RenderFingerprint) -> ReplayPlan:
        """Build a :class:`ReplayPlan` from a recorded fingerprint.

        Re-resolves each reference identity to a fresh URL via the resolver (or
        the recorded-URL fallback). The reconstructed spec carries the exact mode,
        seed, prompt, negative-prompt, duration, resolution, and params the
        original used, so its ``request_digest`` recomputes to the original's when
        every reference resolves cleanly.
        """
        resolver: ReferenceResolver = self._resolver or _IdentityResolver(fp)
        notes: list[str] = []

        resolved_urls: list[str] = []
        unresolved = 0
        for rid in fp.reference_image_ids:
            url = resolver.resolve_reference_url(rid)
            if url == rid and rid not in fp.reference_image_urls:
                # The resolver could not produce a real URL for this identity.
                unresolved += 1
            resolved_urls.append(url)
        if not fp.reference_image_ids:
            # No stable ids recorded → fall back to the recorded URLs verbatim.
            resolved_urls = list(fp.reference_image_urls)
        elif self._resolver is not None and unresolved == 0 and resolved_urls:
            notes.append(f"re-resolved {len(resolved_urls)} reference URL(s)")

        spec = self._spec_for(fp, resolved_urls)

        # Faithfulness: the keyed request is reconstructed iff every reference
        # resolved (so the identity digest is unchanged) and the seed is present.
        faithful = unresolved == 0
        if not faithful:
            notes.append(
                f"{unresolved} reference identit(y/ies) could not be re-resolved; "
                "request is approximate"
            )

        # The reconstructed request digest is computed from the *fingerprint* of
        # the reconstructed spec, but identity is keyed on stable ids (not the
        # freshly-signed URLs), so a faithful re-resolve preserves it. We surface
        # the original fingerprint's request_digest as the target.
        reconstructed_digest = fp.request_digest if faithful else _approx_digest(fp, spec)

        if fp.determinism.label is ReproLabel.GUARANTEED:
            notes.append("model is byte-stable for a fixed request — exact re-render")
        elif fp.determinism.label is ReproLabel.BEST_EFFORT:
            notes.append("model is plan-stable — same composition, pixels may drift")
        else:
            notes.append("model does not honour the seed — re-render is a fresh roll")
        if fp.determinism.version_sensitive:
            notes.append("a model-version change would break reproducibility")

        return ReplayPlan(
            spec=spec,
            provider=fp.provider.provider,
            model=fp.provider.model,
            version=fp.provider.version,
            label=fp.determinism.label,
            faithful=faithful,
            reconstructed_request_digest=reconstructed_digest,
            notes=tuple(notes),
        )

    @staticmethod
    def _spec_for(fp: RenderFingerprint, reference_urls: list[str]) -> WanSpec:
        """Rebuild the :class:`WanSpec` shape implied by the fingerprint's mode.

        Reference URLs are routed into the spec fields the §9.3 mode expects, so
        the reconstructed spec is provider-submittable, not just a record.
        """
        params = fp.params
        spec = WanSpec(
            mode=fp.mode,
            prompt=fp.prompt,
            negative_prompt=fp.negative_prompt,
            seed=fp.seed,
            duration_s=fp.duration_s,
            resolution=fp.resolution,
            watermark=bool(params.get("watermark", False)),
            prompt_extend=bool(params.get("prompt_extend", False)),
            model=fp.provider.model,
            shot_id=fp.shot_id,
        )
        # Place reference URLs per mode (mirrors WanSpec's field semantics).
        if fp.mode is WanMode.REFERENCE_TO_VIDEO:
            spec = spec.model_copy(update={"reference_image_urls": reference_urls})
        elif fp.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            if reference_urls:
                spec = spec.model_copy(update={"image_url": reference_urls[0]})
        elif fp.mode is WanMode.FIRST_LAST_FRAME:
            if len(reference_urls) >= 1:
                spec = spec.model_copy(update={"first_frame_url": reference_urls[0]})
            if len(reference_urls) >= 2:
                spec = spec.model_copy(update={"last_frame_url": reference_urls[1]})
        # TEXT_TO_VIDEO / INSTRUCTION_EDIT carry no reference-image inputs here
        # (source-clip URLs are not part of the keyed identity and are re-resolved
        # by the live render layer when an edit is actually re-issued).
        return spec


def _approx_digest(fp: RenderFingerprint, spec: WanSpec) -> str:
    """Best-effort request digest when a faithful reconstruction was impossible.

    Re-keys the identity on whatever URLs the spec ended up with, so the caller
    can still see that the digest *diverged* from the original (and by how much,
    via a :func:`app.video.repro.diff` comparison of the two fingerprints).
    """
    from .hashing import digest, digest_fields

    identity = digest(
        {"refs": list(spec.reference_image_urls), "voice": spec.reference_voice_url}
    )
    return digest_fields(
        provider=fp.provider.provider,
        model=fp.provider.model,
        version=fp.provider.version,
        protocol=fp.provider.protocol,
        mode=str(fp.mode.value),
        seed=fp.seed,
        duration_s=fp.duration_s,
        resolution=fp.resolution,
        params=fp.params,
        prompt_digest=fp.prompt_digest,
        reference_identity_digest=identity,
    )


__all__ = [
    "ReferenceResolver",
    "RenderReplay",
    "ReplayPlan",
]
