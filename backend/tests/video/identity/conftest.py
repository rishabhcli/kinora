"""Deterministic fixtures + fakes for the identity-conditioning tests.

No network, no infra, no randomness. The fakes implement the injectable seams
(:class:`KeyframeBaker`, :class:`CropEmbedder`) with fully deterministic outputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.video.identity import (
    IdentityBundle,
    LockedReference,
    Pose,
)

# --------------------------------------------------------------------------- #
# Deterministic vectors (tiny, fixed) so cosine math is hand-checkable
# --------------------------------------------------------------------------- #


def unit(*xs: float) -> tuple[float, ...]:
    """Unit-normalize a small vector (so cosine == dot in assertions)."""
    norm = math.sqrt(sum(x * x for x in xs))
    if norm == 0.0:
        return tuple(xs)
    return tuple(x / norm for x in xs)


#: A 3-d "appearance" space. FRONT/3Q point mostly along axis-0 (the entity),
#: PROFILE is a small rotation, an OFF vector is orthogonal (max drift).
ELSA_APPEARANCE = unit(1.0, 0.0, 0.0)
ELSA_FRONT = unit(0.98, 0.20, 0.0)
ELSA_3Q = unit(0.95, 0.30, 0.05)
ELSA_PROFILE = unit(0.80, 0.50, 0.10)
#: Anti-correlated with every Elsa vector (cosine < 0 → clamps to max drift).
ORTHOGONAL = unit(-1.0, 0.0, 0.0)
PNG = b"\x89PNG\r\n\x1a\n" + b"front-bytes"
PNG_3Q = b"\x89PNG\r\n\x1a\n" + b"3q-bytes"
PNG_PROFILE = b"\x89PNG\r\n\x1a\n" + b"profile-bytes"


def make_bundle(
    *,
    with_bytes: bool = True,
    with_descriptors: bool = True,
    with_appearance_embedding: bool = True,
    with_character_id: bool = False,
    with_voice: bool = True,
    urls: bool = True,
) -> IdentityBundle:
    """A fully-featured Elsa bundle; flags strip pieces to exercise degradation."""
    refs = [
        LockedReference(
            ref_id="char_elsa@v3:front",
            pose=Pose.FRONT,
            url="https://oss/elsa/front.png" if urls else None,
            image_bytes=PNG if with_bytes else None,
            descriptor=ELSA_FRONT if with_descriptors else (),
            quality=0.95,
        ),
        LockedReference(
            ref_id="char_elsa@v3:3q",
            pose=Pose.THREE_QUARTER,
            url="https://oss/elsa/3q.png" if urls else None,
            image_bytes=PNG_3Q if with_bytes else None,
            descriptor=ELSA_3Q if with_descriptors else (),
            quality=0.90,
        ),
        LockedReference(
            ref_id="char_elsa@v3:profile",
            pose=Pose.PROFILE,
            url="https://oss/elsa/profile.png" if urls else None,
            image_bytes=PNG_PROFILE if with_bytes else None,
            descriptor=ELSA_PROFILE if with_descriptors else (),
            quality=0.85,
        ),
    ]
    return IdentityBundle(
        entity_key="char_elsa",
        entity_type="character",
        name="Elsa",
        version=3,
        references=tuple(refs),
        appearance_descriptor=ELSA_APPEARANCE if with_appearance_embedding else (),
        character_id="ipadapter_elsa_8f2a" if with_character_id else None,
        appearance_prompt="platinum braid, ice-blue gown, pale skin",
        negative_tokens=("warped face", "extra fingers"),
        voice_ref_url="https://oss/elsa/voice.wav" if with_voice else None,
    )


@dataclass
class BakeCall:
    """A recorded :meth:`FakeBaker.generate` invocation (typed for assertions)."""

    prompt: str
    n_refs: int
    negative_prompt: str | None
    seed: int | None


class FakeBaker:
    """A deterministic image-gen seam: returns a fixed keyframe, records calls."""

    def __init__(self, *, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls: list[BakeCall] = []
        self.output = b"\x89PNG\r\n\x1a\n" + b"baked-keyframe"

    async def generate(
        self,
        prompt: str,
        *,
        reference_images: list[bytes] | None = None,
        negative_prompt: str | None = None,
        seed: int | None = None,
    ) -> list[bytes]:
        self.calls.append(
            BakeCall(
                prompt=prompt,
                n_refs=len(reference_images or []),
                negative_prompt=negative_prompt,
                seed=seed,
            )
        )
        if self.fail:
            raise RuntimeError("image-gen boom")
        if self.empty:
            return []
        return [self.output]


class FakeEmbedder:
    """A deterministic embedder: maps known crop bytes to fixed unit vectors."""

    def __init__(
        self, mapping: dict[bytes, tuple[float, ...]] | None = None, *, fail: bool = False
    ) -> None:
        self.mapping = mapping or {}
        self.fail = fail
        self.calls: list[bytes] = []

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embed boom")
        out: list[list[float]] = []
        for img in images:
            self.calls.append(img)
            vec = self.mapping.get(img, ())
            out.append(list(vec))
        return out
