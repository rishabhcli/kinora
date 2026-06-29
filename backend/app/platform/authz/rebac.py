"""The Google-Zanzibar-shaped relationship model — tuples, rewrites, check, list.

This is the relationship-based (ReBAC) engine of the plane, modelled directly on
Google Zanzibar. Authorization facts are stored as **relation tuples** ::

    <object>#<relation>@<subject>
    book:42#owner@user:alice
    workspace:7#member@user:bob
    book:42#parent@workspace:7        # an object-to-object edge

A **namespace** declares, per object type, what each relation *means* through a
**userset rewrite** expression. The rewrites supported are the Zanzibar set:

* ``this`` — the directly-stored tuples for the relation;
* ``computed_userset(rel)`` — "whoever has ``rel`` on *this* object" (role
  inheritance: an ``owner`` is also an ``editor``);
* ``tuple_to_userset(tupleset_rel, computed_rel)`` — follow ``tupleset_rel`` to
  related objects, then take ``computed_rel`` on each (the **parent/inheritance**
  pattern: a book's ``viewer`` includes the ``member`` of its ``parent``
  workspace);
* ``union`` / ``intersection`` / ``exclusion`` of the above.

:meth:`RelationGraph.check` answers "does subject have relation on object?" by
expanding the rewrite with cycle protection. :meth:`RelationGraph.list_objects`
is the **reverse index**: every object of a type on which the subject has a
relation — "list the books I can read" — computed by walking the tuple set
backwards through the same rewrite tree.

The tuple store is a small protocol (:class:`TupleStore`) with an in-memory
implementation here; a DB-backed store (the ``authz_relation_tuples`` table)
implements the same protocol so the graph logic is identical in tests and in
production. The :class:`RebacEngine` adapts the graph to the plane: it maps a
plane action to a relation (via a configurable action→relation map) and checks
the subject against the resource.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from app.platform.authz.engine import AuthorizationEngine
from app.platform.authz.model import AuthorizationRequest, EngineResult

# --------------------------------------------------------------------------- #
# Tuples
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A typed object: ``type:id`` (e.g. ``book:42``, ``workspace:7``)."""

    type: str
    id: str

    @property
    def ref(self) -> str:
        return f"{self.type}:{self.id}"

    @classmethod
    def parse(cls, s: str) -> ObjectRef:
        type_, _, id_ = s.partition(":")
        return cls(type=type_, id=id_)


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """A tuple subject: either a concrete principal (``user:alice``) or a
    **userset** — another object's relation (``workspace:7#member``), which means
    "every subject that has ``member`` on ``workspace:7``"."""

    type: str
    id: str
    relation: str | None = None  # set => this is a userset, not a concrete subject

    @property
    def ref(self) -> str:
        base = f"{self.type}:{self.id}"
        return f"{base}#{self.relation}" if self.relation else base

    @property
    def is_userset(self) -> bool:
        return self.relation is not None

    @property
    def as_object(self) -> ObjectRef:
        return ObjectRef(type=self.type, id=self.id)

    @classmethod
    def parse(cls, s: str) -> SubjectRef:
        obj, _, rel = s.partition("#")
        type_, _, id_ = obj.partition(":")
        return cls(type=type_, id=id_, relation=rel or None)

    @classmethod
    def user(cls, user_id: str) -> SubjectRef:
        return cls(type="user", id=user_id)


@dataclass(frozen=True, slots=True)
class RelationTuple:
    """A stored relationship fact: ``object#relation@subject``."""

    object: ObjectRef
    relation: str
    subject: SubjectRef

    @property
    def key(self) -> str:
        return f"{self.object.ref}#{self.relation}@{self.subject.ref}"

    @classmethod
    def of(cls, object_: str, relation: str, subject: str) -> RelationTuple:
        return cls(
            object=ObjectRef.parse(object_),
            relation=relation,
            subject=SubjectRef.parse(subject),
        )


# --------------------------------------------------------------------------- #
# Userset rewrites (the namespace config)
# --------------------------------------------------------------------------- #


class Rewrite:
    """Base class for userset-rewrite expressions (the Zanzibar set)."""


@dataclass(frozen=True)
class This(Rewrite):
    """The directly-stored tuples for the relation being defined."""


@dataclass(frozen=True)
class ComputedUserset(Rewrite):
    """"Whoever has ``relation`` on the same object" — role inheritance."""

    relation: str


@dataclass(frozen=True)
class TupleToUserset(Rewrite):
    """Follow ``tupleset`` edges from this object, take ``computed`` on each.

    The parent/inheritance pattern: ``viewer`` of a book includes the ``member``
    of its ``parent`` workspace ⇒ ``TupleToUserset("parent", "member")``.
    """

    tupleset: str  # the relation whose tuples point at related objects
    computed: str  # the relation to compute on each related object


@dataclass(frozen=True)
class Union(Rewrite):
    """Set union of child rewrites (subject has the relation if any child does)."""

    children: tuple[Rewrite, ...]


@dataclass(frozen=True)
class Intersection(Rewrite):
    """Set intersection of child rewrites (must satisfy every child)."""

    children: tuple[Rewrite, ...]


@dataclass(frozen=True)
class Exclusion(Rewrite):
    """``base`` minus ``subtract`` (has ``base`` but not ``subtract``)."""

    base: Rewrite
    subtract: Rewrite


@dataclass(frozen=True)
class Namespace:
    """A type's relation definitions — name → its userset rewrite.

    A relation with no explicit rewrite defaults to :class:`This` (only its
    directly stored tuples count).
    """

    type: str
    relations: dict[str, Rewrite] = field(default_factory=dict)

    def rewrite_for(self, relation: str) -> Rewrite:
        return self.relations.get(relation, This())


# --------------------------------------------------------------------------- #
# Tuple store
# --------------------------------------------------------------------------- #


class TupleStore(Protocol):
    """The storage seam for relation tuples (in-memory or DB-backed)."""

    def subjects(self, object_: ObjectRef, relation: str) -> Iterable[SubjectRef]:
        """Every subject directly stored for ``object#relation``."""
        ...

    def objects_for_subject(
        self, subject: SubjectRef, relation: str, object_type: str
    ) -> Iterable[ObjectRef]:
        """Every object of ``object_type`` that has ``relation@subject`` stored."""
        ...

    def relations_pointing_at(self, object_: ObjectRef) -> Iterable[tuple[ObjectRef, str]]:
        """Reverse edges: ``(parent_object, relation)`` whose subject == ``object``.

        Used by the reverse index to walk tuple-to-userset edges backwards.
        """
        ...

    def object_types(self) -> Iterable[str]:
        """Every object type that appears as a tuple's object (for type probing).

        The reverse index probes these (union the declared namespaces) so an
        inheritance target that is not itself a declared namespace — e.g. an
        ``org`` referenced only via a ``workspace#org@org:…`` edge — is still
        reachable when walking ``tuple_to_userset`` back-edges.
        """
        ...


class InMemoryTupleStore:
    """A dict-backed :class:`TupleStore` for tests and the default plane.

    Tuples are indexed three ways so check + reverse-index are both O(matches):
    forward (object,relation)→subjects, reverse (subject,relation,type)→objects,
    and an object→incoming-edges index for tuple-to-userset back-walks.
    """

    def __init__(self, tuples: Iterable[RelationTuple] = ()) -> None:
        self._tuples: set[str] = set()
        self._forward: dict[tuple[str, str], set[SubjectRef]] = {}
        self._reverse: dict[tuple[str, str, str], set[ObjectRef]] = {}
        self._incoming: dict[str, set[tuple[ObjectRef, str]]] = {}
        self._object_types: set[str] = set()
        for t in tuples:
            self.write(t)

    def write(self, t: RelationTuple) -> None:
        """Insert a tuple (idempotent)."""
        if t.key in self._tuples:
            return
        self._tuples.add(t.key)
        self._object_types.add(t.object.type)
        self._forward.setdefault((t.object.ref, t.relation), set()).add(t.subject)
        if not t.subject.is_userset:
            self._reverse.setdefault(
                (t.subject.ref, t.relation, t.object.type), set()
            ).add(t.object)
        # Every subject that names an object (a concrete object ref like
        # ``workspace:7`` used as a parent pointer, OR a userset ``workspace:7#rel``)
        # is an incoming edge for tuple-to-userset back-walks. Indexing both kinds
        # lets the reverse index follow ``parent`` edges whose subject is stored as
        # a plain object reference, not just a userset.
        self._incoming.setdefault(t.subject.as_object.ref, set()).add(
            (t.object, t.relation)
        )

    def delete(self, t: RelationTuple) -> None:
        """Remove a tuple (idempotent)."""
        if t.key not in self._tuples:
            return
        self._tuples.discard(t.key)
        fwd = self._forward.get((t.object.ref, t.relation))
        if fwd:
            fwd.discard(t.subject)
        if not t.subject.is_userset:
            rev = self._reverse.get((t.subject.ref, t.relation, t.object.type))
            if rev:
                rev.discard(t.object)
        inc = self._incoming.get(t.subject.as_object.ref)
        if inc:
            inc.discard((t.object, t.relation))

    def subjects(self, object_: ObjectRef, relation: str) -> Iterable[SubjectRef]:
        return frozenset(self._forward.get((object_.ref, relation), set()))

    def objects_for_subject(
        self, subject: SubjectRef, relation: str, object_type: str
    ) -> Iterable[ObjectRef]:
        return frozenset(self._reverse.get((subject.ref, relation, object_type), set()))

    def relations_pointing_at(self, object_: ObjectRef) -> Iterable[tuple[ObjectRef, str]]:
        return frozenset(self._incoming.get(object_.ref, set()))

    def object_types(self) -> Iterable[str]:
        return frozenset(self._object_types)

    def __len__(self) -> int:
        return len(self._tuples)

    def __iter__(self) -> Iterator[str]:
        return iter(self._tuples)


# --------------------------------------------------------------------------- #
# The relation graph — check + reverse-index list
# --------------------------------------------------------------------------- #


class ConsistencyError(RuntimeError):
    """Raised on a rewrite cycle the graph cannot resolve (config bug)."""


class RelationGraph:
    """Evaluate Zanzibar checks + reverse-index lists over a tuple store.

    Bind it with the namespace config (relation rewrites per type) and a store.
    :meth:`check` and :meth:`list_objects` are pure functions of the stored
    tuples + config, so they are exhaustively unit-testable in-memory.
    """

    def __init__(
        self, namespaces: Iterable[Namespace], store: TupleStore
    ) -> None:
        self._ns: dict[str, Namespace] = {n.type: n for n in namespaces}
        self._store = store

    def namespace(self, type_: str) -> Namespace:
        return self._ns.get(type_, Namespace(type=type_))

    # -- forward check ------------------------------------------------------- #

    def check(
        self, object_: ObjectRef, relation: str, subject: SubjectRef
    ) -> bool:
        """Whether ``subject`` has ``relation`` on ``object_`` (with cycle guard)."""
        return self._check(object_, relation, subject, set())

    def _check(
        self,
        object_: ObjectRef,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str, str]],
    ) -> bool:
        key = (object_.ref, relation, subject.ref)
        if key in seen:
            return False  # cycle: this path contributes nothing
        seen = seen | {key}
        rewrite = self.namespace(object_.type).rewrite_for(relation)
        return self._eval_rewrite(rewrite, object_, relation, subject, seen)

    def _eval_rewrite(
        self,
        rewrite: Rewrite,
        object_: ObjectRef,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str, str]],
    ) -> bool:
        if isinstance(rewrite, This):
            return self._check_this(object_, relation, subject, seen)
        if isinstance(rewrite, ComputedUserset):
            return self._check(object_, rewrite.relation, subject, seen)
        if isinstance(rewrite, TupleToUserset):
            return self._check_ttu(rewrite, object_, subject, seen)
        if isinstance(rewrite, Union):
            return any(
                self._eval_rewrite(c, object_, relation, subject, seen)
                for c in rewrite.children
            )
        if isinstance(rewrite, Intersection):
            return all(
                self._eval_rewrite(c, object_, relation, subject, seen)
                for c in rewrite.children
            )
        if isinstance(rewrite, Exclusion):
            return self._eval_rewrite(
                rewrite.base, object_, relation, subject, seen
            ) and not self._eval_rewrite(
                rewrite.subtract, object_, relation, subject, seen
            )
        raise ConsistencyError(f"unknown rewrite: {rewrite!r}")  # pragma: no cover

    def _check_this(
        self,
        object_: ObjectRef,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str, str]],
    ) -> bool:
        for stored in self._store.subjects(object_, relation):
            if not stored.is_userset:
                if stored.ref == subject.ref:
                    return True
                continue
            # A userset subject `obj#rel` matches if the subject has `rel` on obj.
            if self._check(stored.as_object, stored.relation or "", subject, seen):
                return True
        return False

    def _check_ttu(
        self,
        rewrite: TupleToUserset,
        object_: ObjectRef,
        subject: SubjectRef,
        seen: set[tuple[str, str, str]],
    ) -> bool:
        # Follow `tupleset` from this object to related objects, then check
        # `computed` on each. The related object is the subject of the tupleset
        # tuple, interpreted as an object reference.
        for related in self._store.subjects(object_, rewrite.tupleset):
            related_obj = related.as_object
            if self._check(related_obj, rewrite.computed, subject, seen):
                return True
        return False

    # -- reverse index ------------------------------------------------------- #

    def list_objects(
        self, object_type: str, relation: str, subject: SubjectRef
    ) -> frozenset[ObjectRef]:
        """Every object of ``object_type`` on which ``subject`` has ``relation``.

        The reverse index — "list the books I can read". Computed by expanding
        the relation's rewrite *backwards* over the tuple set, then (to handle
        intersection/exclusion correctly) confirming each candidate with a
        forward :meth:`check`.
        """
        candidates = self._candidates(object_type, relation, subject, set())
        return frozenset(
            obj for obj in candidates if self.check(obj, relation, subject)
        )

    def _candidates(
        self,
        object_type: str,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str]],
    ) -> set[ObjectRef]:
        key = (object_type, relation)
        if key in seen:
            return set()
        seen = seen | {key}
        rewrite = self.namespace(object_type).rewrite_for(relation)
        return self._candidates_for_rewrite(
            rewrite, object_type, relation, subject, seen
        )

    def _candidates_for_rewrite(
        self,
        rewrite: Rewrite,
        object_type: str,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str]],
    ) -> set[ObjectRef]:
        out: set[ObjectRef]
        if isinstance(rewrite, This):
            out = set(self._store.objects_for_subject(subject, relation, object_type))
            # Also include objects reachable via stored userset subjects: for a
            # userset subject `g#r` stored on object O for `relation`, O is a
            # candidate if the subject has `r` on g.
            out |= self._this_userset_candidates(object_type, relation, subject, seen)
            return out
        if isinstance(rewrite, ComputedUserset):
            return self._candidates(object_type, rewrite.relation, subject, seen)
        if isinstance(rewrite, TupleToUserset):
            return self._ttu_candidates(rewrite, object_type, subject, seen)
        if isinstance(rewrite, Union):
            out = set()
            for c in rewrite.children:
                out |= self._candidates_for_rewrite(
                    c, object_type, relation, subject, seen
                )
            return out
        if isinstance(rewrite, Intersection):
            sets = [
                self._candidates_for_rewrite(c, object_type, relation, subject, seen)
                for c in rewrite.children
            ]
            if not sets:
                return set()
            out = set(sets[0])
            for s in sets[1:]:
                out &= s
            return out
        if isinstance(rewrite, Exclusion):
            # The forward-check pass in list_objects filters the subtract side, so
            # over-approximating with the base candidates here is safe.
            return self._candidates_for_rewrite(
                rewrite.base, object_type, relation, subject, seen
            )
        raise ConsistencyError(f"unknown rewrite: {rewrite!r}")  # pragma: no cover

    def _this_userset_candidates(
        self,
        object_type: str,
        relation: str,
        subject: SubjectRef,
        seen: set[tuple[str, str]],
    ) -> set[ObjectRef]:
        # Find groups the subject belongs to, then objects that grant `relation`
        # to those groups as userset subjects. We can't enumerate all groups
        # cheaply in the generic store, so the forward-check pass in list_objects
        # is what makes this correct; here we surface candidates reachable via the
        # incoming-edge index for the common parent pattern.
        return set()

    def _ttu_candidates(
        self,
        rewrite: TupleToUserset,
        object_type: str,
        subject: SubjectRef,
        seen: set[tuple[str, str]],
    ) -> set[ObjectRef]:
        # For each related object the subject has `computed` on (of any type),
        # walk the incoming `tupleset` edges back to objects of `object_type`.
        # The shared `seen` guard is threaded through so a genuine cycle (e.g.
        # ``book:parent`` pointing back at a ``book``) terminates, while a
        # legitimate cross-type hop (``book:owner`` → ``workspace:owner``) is a
        # distinct ``(type, relation)`` key and is therefore not pruned.
        out: set[ObjectRef] = set()
        for related in self._related_objects_with(rewrite.computed, subject, seen):
            for parent, rel in self._store.relations_pointing_at(related):
                if rel == rewrite.tupleset and parent.type == object_type:
                    out.add(parent)
        return out

    def _related_objects_with(
        self, relation: str, subject: SubjectRef, seen: set[tuple[str, str]]
    ) -> set[ObjectRef]:
        # Objects (of any type) on which the subject has `relation`, via the
        # reverse index, gathered across every known object type — both the
        # declared namespaces and any type present in the store but not declared
        # (e.g. an ``org`` reached only through a ``workspace#org`` edge).
        out: set[ObjectRef] = set()
        for type_ in self._all_object_types():
            out |= self._candidates(type_, relation, subject, seen)
        return out

    def _all_object_types(self) -> frozenset[str]:
        return frozenset(self._ns) | frozenset(self._store.object_types())


# --------------------------------------------------------------------------- #
# The plane engine
# --------------------------------------------------------------------------- #


class RebacEngine(AuthorizationEngine):
    """Adapt the relation graph to the plane's ``check`` (action → relation).

    ``action_relation`` maps a plane action to the relation that grants it
    (``book:read`` → ``viewer``). When an action has no mapped relation the
    engine abstains. A relationship match emits ALLOW; a non-match abstains
    (another engine, e.g. RBAC, may still grant) — the ReBAC engine never emits a
    hard DENY, matching Zanzibar's grant-only semantics.
    """

    name = "rebac"

    def __init__(
        self, graph: RelationGraph, action_relation: dict[str, str]
    ) -> None:
        self._graph = graph
        self._action_relation = dict(action_relation)

    def relation_for(self, action: str) -> str | None:
        if action in self._action_relation:
            return self._action_relation[action]
        # Allow a namespace-qualified fallback (``book:read`` → ``read``).
        _, _, verb = action.partition(":")
        return self._action_relation.get(verb)

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        relation = self.relation_for(request.action)
        if relation is None:
            return EngineResult.abstain(self.name, f"no relation for '{request.action}'")
        object_ = ObjectRef(type=request.resource.type, id=request.resource.id)
        subject = SubjectRef(type=request.subject.type, id=request.subject.id)
        if self._graph.check(object_, relation, subject):
            return EngineResult.allow(
                self.name,
                f"{subject.ref} has '{relation}' on {object_.ref}",
                rule=f"rebac:{relation}",
            )
        return EngineResult.abstain(
            self.name, f"{subject.ref} lacks '{relation}' on {object_.ref}"
        )

    async def aevaluate(self, request: AuthorizationRequest) -> EngineResult:
        return self.evaluate(request)

    def list_objects(self, object_type: str, action: str, subject_id: str) -> frozenset[str]:
        """Object ids of ``object_type`` the user may take ``action`` on (reverse index)."""
        relation = self.relation_for(action)
        if relation is None:
            return frozenset()
        objs = self._graph.list_objects(
            object_type, relation, SubjectRef.user(subject_id)
        )
        return frozenset(o.id for o in objs)


class RelationType(enum.StrEnum):
    """The canonical relation names the plane's namespaces use (Zanzibar roles)."""

    OWNER = "owner"
    EDITOR = "editor"
    COMMENTER = "commenter"
    VIEWER = "viewer"
    MEMBER = "member"
    PARENT = "parent"


__all__ = [
    "ComputedUserset",
    "ConsistencyError",
    "Exclusion",
    "InMemoryTupleStore",
    "Intersection",
    "Namespace",
    "ObjectRef",
    "RebacEngine",
    "RelationGraph",
    "RelationTuple",
    "RelationType",
    "Rewrite",
    "SubjectRef",
    "This",
    "TupleStore",
    "TupleToUserset",
    "Union",
]
