"""Property-style Zanzibar consistency: list_objects ≡ {o : check(o)} exhaustively.

The reverse index and the forward check are two implementations of the same
predicate; this sweeps a constructed Kinora world and asserts they agree for
every object and every relation — the core Zanzibar invariant.
"""

from __future__ import annotations

import itertools

from app.platform.authz.presets import KINORA_NAMESPACES
from app.platform.authz.rebac import (
    InMemoryTupleStore,
    ObjectRef,
    RelationGraph,
    RelationTuple,
    SubjectRef,
)

# A deterministic world exercising every path: personal owners, workspace
# roles at each lattice level, org ownership, parent inheritance, collections.
WORLD = [
    # org ownership: org:acme owner is alice; workspace:w1 belongs to org:acme
    "workspace:w1|org|org:acme",
    "org:acme|owner|user:alice",
    # workspace direct roles
    "workspace:w1|editor|user:bob",
    "workspace:w1|commenter|user:carol",
    "workspace:w1|viewer|user:dave",
    # books attached to w1 inherit its roles
    "book:b1|parent|workspace:w1",
    "book:b2|parent|workspace:w1",
    # a personally-owned book outside any workspace
    "book:b3|owner|user:erin",
    # a second workspace with its own owner
    "workspace:w2|owner|user:frank",
    "book:b4|parent|workspace:w2",
    # a collection in w1
    "collection:c1|parent|workspace:w1",
]

USERS = ["alice", "bob", "carol", "dave", "erin", "frank", "stranger"]
RELATIONS = ["owner", "editor", "commenter", "viewer"]
BOOKS = ["b1", "b2", "b3", "b4"]
WORKSPACES = ["w1", "w2"]
COLLECTIONS = ["c1"]


def _graph() -> RelationGraph:
    store = InMemoryTupleStore(RelationTuple.of(*t.split("|")) for t in WORLD)
    return RelationGraph(KINORA_NAMESPACES, store)


def test_list_objects_equals_forward_check_for_all() -> None:
    g = _graph()
    surfaces = {
        "book": BOOKS,
        "workspace": WORKSPACES,
        "collection": COLLECTIONS,
    }
    for user, relation in itertools.product(USERS, RELATIONS):
        subject = SubjectRef.user(user)
        for obj_type, ids in surfaces.items():
            listed = {o.id for o in g.list_objects(obj_type, relation, subject)}
            forward = {
                i
                for i in ids
                if g.check(ObjectRef(type=obj_type, id=i), relation, subject)
            }
            assert listed == forward, (
                f"mismatch for {user}#{relation} on {obj_type}: "
                f"listed={listed} forward={forward}"
            )


def test_role_lattice_inheritance_via_parent() -> None:
    g = _graph()
    # bob is workspace editor → editor/commenter/viewer of attached books, not owner
    bob = SubjectRef.user("bob")
    assert g.check(ObjectRef.parse("book:b1"), "editor", bob)
    assert g.check(ObjectRef.parse("book:b1"), "commenter", bob)
    assert g.check(ObjectRef.parse("book:b1"), "viewer", bob)
    assert not g.check(ObjectRef.parse("book:b1"), "owner", bob)


def test_org_owner_is_owner_of_everything_beneath() -> None:
    g = _graph()
    alice = SubjectRef.user("alice")
    # alice owns org:acme → owner of workspace:w1 → owner of its books
    assert g.check(ObjectRef.parse("workspace:w1"), "owner", alice)
    assert g.check(ObjectRef.parse("book:b1"), "owner", alice)
    assert g.check(ObjectRef.parse("book:b1"), "viewer", alice)
    # but not of frank's separate workspace
    assert not g.check(ObjectRef.parse("workspace:w2"), "owner", alice)


def test_stranger_has_nothing() -> None:
    g = _graph()
    stranger = SubjectRef.user("stranger")
    for rel in RELATIONS:
        for b in BOOKS:
            assert not g.check(ObjectRef(type="book", id=b), rel, stranger)


def test_commenter_does_not_get_edit() -> None:
    g = _graph()
    carol = SubjectRef.user("carol")  # workspace commenter
    assert g.check(ObjectRef.parse("book:b1"), "commenter", carol)
    assert g.check(ObjectRef.parse("book:b1"), "viewer", carol)
    assert not g.check(ObjectRef.parse("book:b1"), "editor", carol)
