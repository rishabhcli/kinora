"""Zanzibar-consistency tests for the relationship model (check + reverse index).

These verify the userset-rewrite semantics exhaustively: direct tuples,
computed-userset role inheritance, tuple-to-userset parent inheritance, userset
subjects (groups), union / intersection / exclusion, cycle protection, and the
reverse-index ``list_objects`` agreeing with the forward ``check`` for every
object.
"""

from __future__ import annotations

from app.platform.authz.presets import ACTION_RELATION, KINORA_NAMESPACES
from app.platform.authz.rebac import (
    ComputedUserset,
    Exclusion,
    InMemoryTupleStore,
    Intersection,
    Namespace,
    ObjectRef,
    RebacEngine,
    RelationGraph,
    RelationTuple,
    SubjectRef,
    This,
    TupleToUserset,
    Union,
)


def _graph(namespaces, *tuples: str) -> RelationGraph:
    store = InMemoryTupleStore(
        RelationTuple.of(*t.split("|")) for t in tuples
    )
    return RelationGraph(namespaces, store)


def _user(name: str) -> SubjectRef:
    return SubjectRef.user(name)


# -- tuple parsing ----------------------------------------------------------- #


def test_tuple_parse_roundtrip() -> None:
    t = RelationTuple.of("book:42", "owner", "user:alice")
    assert t.object.ref == "book:42"
    assert t.relation == "owner"
    assert t.subject.ref == "user:alice"
    assert t.key == "book:42#owner@user:alice"


def test_userset_subject_parse() -> None:
    s = SubjectRef.parse("workspace:7#member")
    assert s.is_userset and s.relation == "member"
    assert s.as_object.ref == "workspace:7"


# -- `this`: direct tuples --------------------------------------------------- #


def test_direct_tuple_check() -> None:
    ns = [Namespace(type="book", relations={"owner": This()})]
    g = _graph(ns, "book:1|owner|user:alice")
    assert g.check(ObjectRef.parse("book:1"), "owner", _user("alice"))
    assert not g.check(ObjectRef.parse("book:1"), "owner", _user("bob"))


# -- computed_userset: role inheritance -------------------------------------- #


def test_computed_userset_inheritance() -> None:
    ns = [
        Namespace(
            type="book",
            relations={
                "owner": This(),
                "viewer": Union((This(), ComputedUserset("owner"))),
            },
        )
    ]
    g = _graph(ns, "book:1|owner|user:alice")
    # alice is owner → also viewer via computed_userset
    assert g.check(ObjectRef.parse("book:1"), "viewer", _user("alice"))


# -- userset subjects (groups) ----------------------------------------------- #


def test_userset_subject_group_membership() -> None:
    ns = [
        Namespace(type="workspace", relations={"member": This()}),
        Namespace(type="book", relations={"viewer": This()}),
    ]
    # book:1 viewer is "every member of workspace:7"
    g = _graph(
        ns,
        "workspace:7|member|user:bob",
        "book:1|viewer|workspace:7#member",
    )
    assert g.check(ObjectRef.parse("book:1"), "viewer", _user("bob"))
    assert not g.check(ObjectRef.parse("book:1"), "viewer", _user("carol"))


# -- tuple_to_userset: parent inheritance ------------------------------------ #


def test_tuple_to_userset_parent() -> None:
    ns = [
        Namespace(type="workspace", relations={"member": This()}),
        Namespace(
            type="book",
            relations={"viewer": TupleToUserset(tupleset="parent", computed="member")},
        ),
    ]
    g = _graph(
        ns,
        "workspace:7|member|user:bob",
        "book:1|parent|workspace:7",
    )
    # book:1's viewer = member of its parent workspace:7
    assert g.check(ObjectRef.parse("book:1"), "viewer", _user("bob"))
    assert not g.check(ObjectRef.parse("book:1"), "viewer", _user("carol"))


# -- union / intersection / exclusion ---------------------------------------- #


def test_union() -> None:
    ns = [
        Namespace(
            type="doc",
            relations={
                "a": This(),
                "b": This(),
                "view": Union((ComputedUserset("a"), ComputedUserset("b"))),
            },
        )
    ]
    g = _graph(ns, "doc:1|a|user:x", "doc:1|b|user:y")
    assert g.check(ObjectRef.parse("doc:1"), "view", _user("x"))
    assert g.check(ObjectRef.parse("doc:1"), "view", _user("y"))
    assert not g.check(ObjectRef.parse("doc:1"), "view", _user("z"))


def test_intersection() -> None:
    ns = [
        Namespace(
            type="doc",
            relations={
                "a": This(),
                "b": This(),
                "view": Intersection((ComputedUserset("a"), ComputedUserset("b"))),
            },
        )
    ]
    g = _graph(ns, "doc:1|a|user:x", "doc:1|b|user:x", "doc:1|a|user:y")
    assert g.check(ObjectRef.parse("doc:1"), "view", _user("x"))  # has both
    assert not g.check(ObjectRef.parse("doc:1"), "view", _user("y"))  # only a


def test_exclusion() -> None:
    ns = [
        Namespace(
            type="doc",
            relations={
                "member": This(),
                "banned": This(),
                "view": Exclusion(
                    base=ComputedUserset("member"),
                    subtract=ComputedUserset("banned"),
                ),
            },
        )
    ]
    g = _graph(ns, "doc:1|member|user:x", "doc:1|member|user:y", "doc:1|banned|user:y")
    assert g.check(ObjectRef.parse("doc:1"), "view", _user("x"))
    assert not g.check(ObjectRef.parse("doc:1"), "view", _user("y"))  # banned


# -- cycle protection -------------------------------------------------------- #


def test_cycle_protection_terminates() -> None:
    # a relation defined in terms of itself (config bug) must not loop forever
    ns = [Namespace(type="doc", relations={"loop": ComputedUserset("loop")})]
    g = _graph(ns)
    assert not g.check(ObjectRef.parse("doc:1"), "loop", _user("x"))


# -- reverse index: list_objects agrees with check --------------------------- #


def test_list_objects_direct() -> None:
    ns = [Namespace(type="book", relations={"viewer": This()})]
    g = _graph(ns, "book:1|viewer|user:a", "book:2|viewer|user:a", "book:3|viewer|user:b")
    objs = g.list_objects("book", "viewer", _user("a"))
    assert {o.id for o in objs} == {"1", "2"}


def test_list_objects_via_parent_inheritance() -> None:
    ns = [
        Namespace(type="workspace", relations={"member": This()}),
        Namespace(
            type="book",
            relations={"viewer": TupleToUserset(tupleset="parent", computed="member")},
        ),
    ]
    g = _graph(
        ns,
        "workspace:7|member|user:bob",
        "book:1|parent|workspace:7",
        "book:2|parent|workspace:7",
        "book:9|parent|workspace:8",
    )
    objs = g.list_objects("book", "viewer", _user("bob"))
    assert {o.id for o in objs} == {"1", "2"}


def test_list_objects_consistent_with_check_kinora_presets() -> None:
    # Build a small Kinora world and assert list_objects == {b : check(b)}.
    g = _graph(
        KINORA_NAMESPACES,
        "book:own|owner|user:alice",
        "workspace:w|owner|user:alice",
        "book:shared|parent|workspace:w",
        "book:other|owner|user:bob",
        "workspace:w2|editor|user:alice",
        "book:edit|parent|workspace:w2",
    )
    listed = {o.id for o in g.list_objects("book", "viewer", _user("alice"))}
    # cross-check against forward check over every known book
    all_books = {"own", "shared", "other", "edit"}
    forward = {
        b for b in all_books
        if g.check(ObjectRef.parse(f"book:{b}"), "viewer", _user("alice"))
    }
    assert listed == forward
    assert "own" in forward and "shared" in forward and "edit" in forward
    assert "other" not in forward


# -- the plane engine -------------------------------------------------------- #


def test_rebac_engine_maps_action_to_relation() -> None:
    g = _graph(KINORA_NAMESPACES, "book:1|owner|user:alice")
    engine = RebacEngine(g, ACTION_RELATION)
    from app.platform.authz.model import AuthorizationRequest, Resource, Subject

    req = AuthorizationRequest(
        subject=Subject.user("alice"), action="book:read", resource=Resource.of("book", "1")
    )
    res = engine.evaluate(req)
    assert res.effect.value == "allow"


def test_rebac_engine_abstains_for_unmapped_action() -> None:
    g = _graph(KINORA_NAMESPACES)
    engine = RebacEngine(g, ACTION_RELATION)
    from app.platform.authz.model import AuthorizationRequest, Resource, Subject

    req = AuthorizationRequest(
        subject=Subject.user("alice"), action="book:teleport", resource=Resource.of("book", "1")
    )
    assert engine.evaluate(req).effect.value == "abstain"


def test_rebac_engine_list_objects() -> None:
    g = _graph(KINORA_NAMESPACES, "book:1|owner|user:alice", "book:2|owner|user:bob")
    engine = RebacEngine(g, ACTION_RELATION)
    assert engine.list_objects("book", "book:read", "alice") == frozenset({"1"})
    assert engine.list_objects("book", "book:teleport", "alice") == frozenset()


# -- tuple store mutation ---------------------------------------------------- #


def test_store_write_delete_idempotent() -> None:
    store = InMemoryTupleStore()
    t = RelationTuple.of("book:1", "owner", "user:alice")
    store.write(t)
    store.write(t)  # idempotent
    assert len(store) == 1
    store.delete(t)
    store.delete(t)  # idempotent
    assert len(store) == 0
