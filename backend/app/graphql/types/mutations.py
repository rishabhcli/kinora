"""Input + result object types for the gateway's mutations.

Mutations mirror the REST control surface (§4/§5.4) but in GraphQL's
input-object + typed-result style:

* ``createReadingSession`` → a ``Session``;
* ``updateIntent`` → ``IntentResult`` (one control tick: promotions/keyframes);
* ``seek`` → ``SeekResult`` (cancellations + bridge);
* ``directorComment`` → ``CommentResult`` (agent routing + regen job);
* ``editCanon`` → ``CanonEditResult`` (new version + surgical regen blast radius);
* ``resolveConflict`` → ``ConflictResult`` (the §7.2 resolution outcome).

Inputs carry strict types so coercion rejects bad client data before any DB or
provider call; results are plain dicts the resolvers return.
"""

from __future__ import annotations

from typing import Any

from app.graphql.scalars import (
    GraphQLBoolean,
    GraphQLFloat,
    GraphQLID,
    GraphQLInt,
    GraphQLJSON,
    GraphQLString,
)
from app.graphql.type_system import (
    Field,
    GraphQLInputObject,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
    InputField,
)
from app.graphql.types.enums import CONFLICT_OPTION_ENUM, SESSION_MODE_ENUM

_CACHE: dict[str, GraphQLObject] = {}
_INPUTS: dict[str, GraphQLInputObject] = {}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def create_session_input() -> GraphQLInputObject:
    if "CreateReadingSessionInput" not in _INPUTS:
        _INPUTS["CreateReadingSessionInput"] = GraphQLInputObject(
            "CreateReadingSessionInput",
            {
                "bookId": InputField(GraphQLNonNull(GraphQLID)),
                "focusWord": InputField(GraphQLInt, default_value=0),
                "mode": InputField(SESSION_MODE_ENUM, default_value="viewer"),
            },
            description="Open a reading session against a book (§4.9).",
        )
    return _INPUTS["CreateReadingSessionInput"]


def update_intent_input() -> GraphQLInputObject:
    if "UpdateIntentInput" not in _INPUTS:
        _INPUTS["UpdateIntentInput"] = GraphQLInputObject(
            "UpdateIntentInput",
            {
                "sessionId": InputField(GraphQLNonNull(GraphQLID)),
                "focusWord": InputField(GraphQLNonNull(GraphQLInt)),
                "velocity": InputField(GraphQLFloat, default_value=4.0),
                "mode": InputField(SESSION_MODE_ENUM),
            },
            description="A debounced reading-intent update: focus word + velocity (§4.3).",
        )
    return _INPUTS["UpdateIntentInput"]


def seek_input() -> GraphQLInputObject:
    if "SeekInput" not in _INPUTS:
        _INPUTS["SeekInput"] = GraphQLInputObject(
            "SeekInput",
            {
                "sessionId": InputField(GraphQLNonNull(GraphQLID)),
                "word": InputField(GraphQLNonNull(GraphQLInt)),
            },
            description="Jump to a word: cancel distant work, bridge, re-seed (§4.8).",
        )
    return _INPUTS["SeekInput"]


def director_comment_input() -> GraphQLInputObject:
    if "DirectorCommentInput" not in _INPUTS:
        _INPUTS["DirectorCommentInput"] = GraphQLInputObject(
            "DirectorCommentInput",
            {
                "sessionId": InputField(GraphQLNonNull(GraphQLID)),
                "shotId": InputField(GraphQLNonNull(GraphQLID)),
                "note": InputField(GraphQLNonNull(GraphQLString)),
            },
            description="A Director region-comment routed to an agent + regen (§5.4).",
        )
    return _INPUTS["DirectorCommentInput"]


def edit_canon_input() -> GraphQLInputObject:
    if "EditCanonInput" not in _INPUTS:
        _INPUTS["EditCanonInput"] = GraphQLInputObject(
            "EditCanonInput",
            {
                "bookId": InputField(GraphQLNonNull(GraphQLID)),
                "entityKey": InputField(GraphQLNonNull(GraphQLString)),
                "changes": InputField(GraphQLNonNull(GraphQLJSON)),
                "validFromBeat": InputField(GraphQLInt),
            },
            description="Edit a canon entity, triggering surgical dependent regen (§8.7).",
        )
    return _INPUTS["EditCanonInput"]


def resolve_conflict_input() -> GraphQLInputObject:
    if "ResolveConflictInput" not in _INPUTS:
        _INPUTS["ResolveConflictInput"] = GraphQLInputObject(
            "ResolveConflictInput",
            {
                "sessionId": InputField(GraphQLNonNull(GraphQLID)),
                "conflictId": InputField(GraphQLNonNull(GraphQLID)),
                "option": InputField(GraphQLNonNull(CONFLICT_OPTION_ENUM)),
            },
            description="The Director's resolution of a surfaced conflict (§7.2).",
        )
    return _INPUTS["ResolveConflictInput"]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def _result(name: str, builder: Any) -> GraphQLObject:
    if name not in _CACHE:
        _CACHE[name] = builder()
    return _CACHE[name]


def intent_result_type() -> GraphQLObject:
    return _result("IntentResult", _build_intent_result)


def _build_intent_result() -> GraphQLObject:
    return GraphQLObject(
        "IntentResult",
        {
            "sessionId": Field(GraphQLNonNull(GraphQLID), resolver=_g("sessionId")),
            "settled": Field(GraphQLNonNull(GraphQLBoolean), resolver=_g("settled")),
            "allowPromotion": Field(
                GraphQLNonNull(GraphQLBoolean), resolver=_g("allowPromotion")
            ),
            "idle": Field(GraphQLNonNull(GraphQLBoolean), resolver=_g("idle")),
            "bursting": Field(GraphQLNonNull(GraphQLBoolean), resolver=_g("bursting")),
            "committedSecondsAhead": Field(
                GraphQLNonNull(GraphQLFloat), resolver=_g("committedSecondsAhead")
            ),
            "promoted": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLID))),
                resolver=_g("promoted"),
            ),
            "keyframed": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLID))),
                resolver=_g("keyframed"),
            ),
            "cancelled": Field(GraphQLNonNull(GraphQLInt), resolver=_g("cancelled")),
        },
        description="The outcome of one control tick (§4.9).",
    )


def seek_result_type() -> GraphQLObject:
    return _result("SeekResult", _build_seek_result)


def _build_seek_result() -> GraphQLObject:
    return GraphQLObject(
        "SeekResult",
        {
            "sessionId": Field(GraphQLNonNull(GraphQLID), resolver=_g("sessionId")),
            "word": Field(GraphQLNonNull(GraphQLInt), resolver=_g("word")),
            "cancelled": Field(GraphQLNonNull(GraphQLInt), resolver=_g("cancelled")),
            "bridgeBeat": Field(GraphQLID, resolver=_g("bridgeBeat")),
            "committedSecondsAhead": Field(
                GraphQLNonNull(GraphQLFloat), resolver=_g("committedSecondsAhead")
            ),
        },
        description="The outcome of a seek (§4.8).",
    )


def comment_result_type() -> GraphQLObject:
    return _result("CommentResult", _build_comment_result)


def _build_comment_result() -> GraphQLObject:
    return GraphQLObject(
        "CommentResult",
        {
            "shotId": Field(GraphQLNonNull(GraphQLID), resolver=_g("shotId")),
            "agent": Field(GraphQLNonNull(GraphQLString), resolver=_g("agent")),
            "aspect": Field(GraphQLNonNull(GraphQLString), resolver=_g("aspect")),
            "message": Field(GraphQLNonNull(GraphQLString), resolver=_g("message")),
            "jobId": Field(GraphQLString, resolver=_g("jobId")),
            "learned": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLJSON))),
                resolver=_g("learned"),
                description="Directing priors this note taught (§8.6).",
            ),
        },
        description="How a Director comment was routed + the regen it triggered (§5.4).",
    )


def canon_edit_result_type() -> GraphQLObject:
    return _result("CanonEditResult", _build_canon_edit_result)


def _build_canon_edit_result() -> GraphQLObject:
    return GraphQLObject(
        "CanonEditResult",
        {
            "entityKey": Field(GraphQLNonNull(GraphQLString), resolver=_g("entityKey")),
            "version": Field(GraphQLNonNull(GraphQLInt), resolver=_g("version")),
            "affectedShotIds": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLID))),
                resolver=_g("affectedShotIds"),
            ),
            "skippedShots": Field(GraphQLNonNull(GraphQLInt), resolver=_g("skippedShots")),
        },
        description="The new entity version + the dependent shots regenerated (§8.7).",
    )


def conflict_result_type() -> GraphQLObject:
    return _result("ConflictResult", _build_conflict_result)


def _build_conflict_result() -> GraphQLObject:
    return GraphQLObject(
        "ConflictResult",
        {
            "conflictId": Field(GraphQLNonNull(GraphQLID), resolver=_g("conflictId")),
            "option": Field(GraphQLNonNull(CONFLICT_OPTION_ENUM), resolver=_g("option")),
            "status": Field(GraphQLNonNull(GraphQLString), resolver=_g("status")),
            "shotId": Field(GraphQLID, resolver=_g("shotId")),
            "reasoning": Field(GraphQLString, resolver=_g("reasoning")),
        },
        description="The outcome of resolving a continuity conflict (§7.2).",
    )


def _g(key: str) -> Any:
    return lambda s, a, c, i: s.get(key)


__all__ = [
    "canon_edit_result_type",
    "comment_result_type",
    "conflict_result_type",
    "create_session_input",
    "director_comment_input",
    "edit_canon_input",
    "intent_result_type",
    "resolve_conflict_input",
    "seek_input",
    "seek_result_type",
    "update_intent_input",
]
