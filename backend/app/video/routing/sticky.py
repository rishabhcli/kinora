"""Sticky routing — pin a shot family to one backend for visual continuity.

Different hosted video models have different "look" (color, motion, identity
rendering). Within one continuous beat — a *shot family* (e.g. all shots of one
scene/segment) — switching backends mid-stream produces a visible seam. Sticky
routing remembers which backend first served a family and prefers it for every
later shot in that family, so the adaptation stays visually consistent (the same
continuity the canon enforces for content).

The store is a small bounded LRU keyed by an opaque *family key* the router
derives from a :class:`~app.providers.types.WanSpec` (its ``shot_id`` family
prefix, or an explicit family hint). Stickiness is a *preference*, never a
override: the router still skips a pinned backend whose breaker is open and
re-pins to whatever actually served the shot, so a dead provider can't wedge a
whole family.
"""

from __future__ import annotations

from collections import OrderedDict

from app.providers.types import WanSpec


def family_key(spec: WanSpec) -> str | None:
    """Derive a stable shot-family key from a :class:`WanSpec` (pure).

    Uses ``shot_id`` if present. Shot ids in this repo are commonly hierarchical
    (``"<session>:<segment>:<shot>"`` style); the family is everything up to the
    last ``:`` separator so sibling shots of one segment share a key, while a flat
    id falls back to itself. Returns ``None`` when there is no id to key on (the
    render is then routed without stickiness).
    """
    shot_id = spec.shot_id
    if not shot_id:
        return None
    head, sep, _tail = shot_id.rpartition(":")
    return head if sep else shot_id


class StickyStore:
    """A bounded LRU mapping family-key → last backend name that served it.

    Bounded so a long adaptation (thousands of families) can't grow the map
    without limit; the least-recently-used family is evicted at capacity. Pure
    in-memory and process-local — stickiness is a best-effort continuity nudge,
    not durable state, so it intentionally resets on restart.
    """

    def __init__(self, *, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._map: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str | None) -> str | None:
        """The pinned backend for ``key`` (and mark it most-recently-used)."""
        if key is None:
            return None
        backend = self._map.get(key)
        if backend is not None:
            self._map.move_to_end(key)
        return backend

    def set(self, key: str | None, backend: str) -> None:
        """Pin ``key`` to ``backend`` (evicting the LRU family at capacity)."""
        if key is None:
            return
        self._map[key] = backend
        self._map.move_to_end(key)
        while len(self._map) > self._capacity:
            self._map.popitem(last=False)

    def __len__(self) -> int:
        return len(self._map)


def apply_stickiness(
    ranked: list[str],
    pinned: str | None,
) -> list[str]:
    """Promote the ``pinned`` backend to the front of ``ranked`` if present (pure).

    The policy already ranked the routable candidates; if a family was previously
    served by a still-routable backend, prefer it for continuity by moving it to
    the front while preserving the relative order of the rest. A pinned backend
    that is *not* in ``ranked`` (its breaker is open, or it was filtered out as
    incapable) is ignored — stickiness never resurrects an unroutable backend.
    """
    if pinned is None or pinned not in ranked:
        return ranked
    return [pinned] + [name for name in ranked if name != pinned]


__all__ = [
    "StickyStore",
    "apply_stickiness",
    "family_key",
]
