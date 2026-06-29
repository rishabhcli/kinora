"""Symmetry reduction — collapse interchangeable components into one orbit.

Many of our models contain interchangeable parts: two render workers, three
reading sessions, N identical shots in the buffer. If the *property* being
checked is blind to which worker is which (it only ever asks "is **any** worker
holding a lease on this job?"), then a state and any permutation of its
interchangeable components are equivalent — they have the same future up to
renaming. Exploring all permutations is pure waste: the reachable graph blows up
factorially in the number of symmetric components for no new behaviour.

Symmetry reduction picks, from each *orbit* (the set of states reachable from
one another by a permutation of the symmetric components), a single canonical
**representative** and explores only that. The engine, given a
:class:`SymmetryReduction`, canonicalises every state before hashing it, so the
whole orbit collapses to one node.

The contract the caller must honour: the supplied ``canonicalize`` must map a
state to a unique representative of its orbit *and* commute with the model — if
``s -> s'`` is an edge, then ``canon(s) -> canon(s')`` must also be an edge of
the model (true exactly when the actions and properties are themselves
symmetric in the components). This module does not *verify* that contract — it
gives you the machinery and a couple of ready-made canonicalisers (sorting a
multiset of component states is the common, always-sound case) and trusts the
spec author. A wrong canonicaliser can hide states, so the specs in
:mod:`app.verification.specs` only use the provably-sound "sort the multiset of
symmetric slots" form.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

#: A canonicaliser is a pure ``state -> representative`` map. It is intentionally
#: state-agnostic (``object -> object``): the engine binds its own ``StateT`` at
#: the call site and treats the returned representative as a state of that type,
#: so a reduction can be reused across models.
Canonicalizer = Callable[[Any], Any]

__all__ = ["Canonicalizer", "SymmetryReduction", "sort_multiset"]


class _Comparable(Protocol):
    def __lt__(self, other: Any, /) -> bool: ...


ItemT = TypeVar("ItemT", bound=_Comparable)


def sort_multiset(items: Iterable[ItemT]) -> tuple[ItemT, ...]:
    """Canonicalise an interchangeable collection by sorting it.

    The always-sound symmetry canonicaliser: if a tuple of slots is fully
    interchangeable (the model never refers to slot *index*, only to the
    multiset of slot contents), then sorting the slots maps every permutation of
    the orbit to one representative. Use this on the part of a state that is a
    bag of identical components (the lease-holders, the per-session buffers).

    Items must be mutually sortable; the common case is tuples of comparable
    primitives, which sort lexicographically.
    """
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class SymmetryReduction(Sequence[object]):
    """A canonicaliser the engine applies to every state before hashing.

    ``canonicalize`` maps a state to the representative of its symmetry orbit.
    The identity reduction (:meth:`none`) is a no-op for models without symmetry.
    Multiple reductions compose with :meth:`then` (apply this, then the next),
    which is how a model symmetric in *both* workers *and* sessions canonicalises
    along both axes.

    The :class:`~collections.abc.Sequence` base is a small ergonomic hook so a
    reduction can be passed transparently where the engine accepts either one
    reduction or a chain; it exposes the composed chain as a read-only sequence
    for introspection.
    """

    canonicalize: Canonicalizer
    description: str = "symmetry"
    _chain: tuple[SymmetryReduction, ...] = ()

    @classmethod
    def none(cls) -> SymmetryReduction:
        """The identity reduction (explore the full graph)."""
        return cls(canonicalize=lambda s: s, description="none")

    @classmethod
    def by(
        cls, canonicalize: Canonicalizer, *, description: str = "symmetry"
    ) -> SymmetryReduction:
        """Build a reduction from a canonicalising function."""
        return cls(canonicalize=canonicalize, description=description)

    def apply(self, state: Any) -> Any:
        """Canonicalise ``state`` to its orbit representative."""
        result = self.canonicalize(state)
        for nxt in self._chain:
            result = nxt.canonicalize(result)
        return result

    def then(self, other: SymmetryReduction) -> SymmetryReduction:
        """Compose: apply this reduction, then ``other`` (both must be sound)."""
        return SymmetryReduction(
            canonicalize=self.canonicalize,
            description=f"{self.description}+{other.description}",
            _chain=(*self._chain, other),
        )

    @property
    def is_identity(self) -> bool:
        return self.description == "none" and not self._chain

    # -- Sequence introspection -------------------------------------------- #

    def _links(self) -> tuple[SymmetryReduction, ...]:
        return (self, *self._chain)

    def __len__(self) -> int:
        return len(self._links())

    def __getitem__(self, index: int) -> object:  # type: ignore[override]
        return self._links()[index]
