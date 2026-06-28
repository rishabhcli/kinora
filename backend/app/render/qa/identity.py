"""Per-character identity verification at scale (§9.5 Identity check, §13 CCS).

The §9.5 Critic's identity check is a single CCS — cosine of *the* character crop
against *the* locked reference. That collapses the moment a shot has more than one
locked character: averaging a strong protagonist with a wrong-faced background
character hides the very failure the check exists to catch. §13 defines CCS as the
*mean* appearance similarity for a character across the shots they appear in, which
is a per-character quantity, not a per-shot scalar.

This module computes a **per-character CCS vector** for one clip — one similarity
per present locked character — and aggregates with a *weakest-link* gate: the shot's
identity score is the **minimum** across present characters, so one wrong face fails
the shot even in a twelve-character crowd. It also supports multiple crops per
character (e.g. a character appearing in several frames) by taking that character's
best-matching crop, which is robust to occlusion / a bad single frame.

Everything here is pure over already-extracted crops + an injected
:class:`~app.memory.interfaces.Embedder` (the same seam the Critic already uses), so
no network call is forced and the logic is unit-testable with a fake embedder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.providers import cosine

#: The §9.5 pre-registered per-character identity floor (CCS ≥ 0.85).
DEFAULT_CCS_MIN = 0.85


@dataclass(frozen=True, slots=True)
class CharacterCrops:
    """One present locked character: their reference image(s) + crop(s) from the clip.

    ``ref_images`` are the locked appearance references for the character (one or
    more poses); ``crops`` are the detected crops of that character pulled from the
    clip's frames. Both are raw image bytes the embedder can embed.
    """

    character_key: str
    ref_images: list[bytes] = field(default_factory=list)
    crops: list[bytes] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CharacterIdentity:
    """The identity verdict for one character in one clip."""

    character_key: str
    ccs: float
    n_crops: int
    n_refs: int
    present: bool
    ok: bool


@dataclass(frozen=True, slots=True)
class IdentityReport:
    """The whole clip's identity verdict: per-character CCS + a weakest-link gate."""

    per_character: list[CharacterIdentity] = field(default_factory=list)
    #: Aggregate CCS = min over present characters (one wrong face gates the shot).
    aggregate_ccs: float = 1.0
    #: The character that gated the shot (lowest CCS), if any present.
    weakest_character: str | None = None
    ok: bool = True

    def ccs_map(self) -> dict[str, float]:
        """A ``{character_key: ccs}`` mapping for the ``QARecord.per_character_ccs``."""
        return {c.character_key: round(c.ccs, 4) for c in self.per_character if c.present}


def _best_match_ccs(
    crops: list[list[float]], refs: list[list[float]]
) -> float:
    """The best crop↔ref cosine for one character (robust to one bad crop/pose).

    For each detected crop we take its closest reference pose, then take the best
    such crop — so a character verified by *any* clean frame against *any* locked
    pose passes, which matches how a human checks "is that them?" across the clip.
    """
    best = 0.0
    for crop_vec in crops:
        for ref_vec in refs:
            best = max(best, cosine(crop_vec, ref_vec))
    return best


async def verify_identities(
    characters: list[CharacterCrops],
    *,
    embedder: object,
    ccs_min: float = DEFAULT_CCS_MIN,
) -> IdentityReport:
    """Compute the per-character CCS vector + weakest-link gate for one clip (§9.5/§13).

    ``embedder`` must expose ``embed_images(list[bytes]) -> list[list[float]]`` (the
    :class:`~app.memory.interfaces.Embedder` seam). A character with no detected crop
    is recorded as *not present* and does not gate the shot (it simply isn't on
    screen); a character present but with no locked reference is treated as N/A
    (CCS 1.0) exactly like the single-character path's "no locked ref" branch.
    """
    embed = getattr(embedder, "embed_images", None)
    if embed is None:
        # No embedder → identity check is N/A (mirrors Critic._ccs's None branch).
        return IdentityReport(aggregate_ccs=1.0, ok=True)

    results: list[CharacterIdentity] = []
    present_scores: list[tuple[str, float]] = []
    for char in characters:
        present = bool(char.crops)
        if not present:
            results.append(
                CharacterIdentity(
                    character_key=char.character_key,
                    ccs=1.0,
                    n_crops=0,
                    n_refs=len(char.ref_images),
                    present=False,
                    ok=True,
                )
            )
            continue
        if not char.ref_images:
            # Present but no locked reference to verify against → N/A.
            results.append(
                CharacterIdentity(
                    character_key=char.character_key,
                    ccs=1.0,
                    n_crops=len(char.crops),
                    n_refs=0,
                    present=True,
                    ok=True,
                )
            )
            continue
        crop_vecs = await embed(char.crops)
        ref_vecs = await embed(char.ref_images)
        ccs = _best_match_ccs(crop_vecs, ref_vecs)
        ok = ccs >= ccs_min
        results.append(
            CharacterIdentity(
                character_key=char.character_key,
                ccs=ccs,
                n_crops=len(char.crops),
                n_refs=len(char.ref_images),
                present=True,
                ok=ok,
            )
        )
        present_scores.append((char.character_key, ccs))

    if not present_scores:
        return IdentityReport(per_character=results, aggregate_ccs=1.0, ok=True)
    weakest_key, aggregate = min(present_scores, key=lambda kv: kv[1])
    return IdentityReport(
        per_character=results,
        aggregate_ccs=round(aggregate, 6),
        weakest_character=weakest_key,
        ok=aggregate >= ccs_min,
    )


__all__ = [
    "DEFAULT_CCS_MIN",
    "CharacterCrops",
    "CharacterIdentity",
    "IdentityReport",
    "verify_identities",
]
