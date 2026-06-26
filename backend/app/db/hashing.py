"""Content-hash for shots (kinora.md §8.7).

The hash is the render queue's idempotency key and the shot cache's key:

    shot_hash = sha1(book_id + beat_id + canon_version_at_render
                     + render_mode + seed + reference_set_hash)

Two shots with identical inputs hash identically (a cache hit serves the cached
clip and spends zero video-seconds); change any input — e.g. a Director edits a
character so ``reference_set_hash`` changes — and only the dependent shots get a
new hash and re-render.

Implementation note: because this value is used as a *cache key / idempotency
key*, the components are joined with a unit-separator (``\\x1f``) that cannot
appear in the textual inputs. This preserves the spec's "concatenation" while
removing the boundary ambiguity that plain concatenation would introduce (e.g.
``book="a", beat="bc"`` vs ``book="ab", beat="c"``). The function is pure and
deterministic.
"""

from __future__ import annotations

import hashlib

_SEP = "\x1f"

# Golden-ratio seed step: a regen re-rolls to a genuinely new variation while
# staying deterministic. Masked to a non-negative 31-bit int.
_SEED_STEP = 0x9E3779B1
_SEED_MASK = 0x7FFFFFFF


def rotate_seed(seed: int | None) -> int:
    """Advance a render seed to the next deterministic variation (§8.7)."""
    return (int(seed or 0) + _SEED_STEP) & _SEED_MASK


def compute_shot_hash(
    *,
    book_id: str,
    beat_id: str,
    canon_version_at_render: int,
    render_mode: str,
    seed: int,
    reference_set_hash: str,
) -> str:
    """Return the deterministic SHA-1 content hash for a shot's render inputs."""
    payload = _SEP.join(
        (
            book_id,
            beat_id,
            str(canon_version_at_render),
            render_mode,
            str(seed),
            reference_set_hash,
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
