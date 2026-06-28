"""Optional LLM refinement of the deterministic comprehension (§4.2, §10).

The deterministic engine (:mod:`app.agents.comprehension.engine`) is the
always-available floor. This module adds a *bounded, opt-in* refinement: a single
JSON-strict model call that may correct a beat's POV / discourse / tempo,
attribute dialogue the heuristic left blank, and add device → visual-intent
translations the lexical scanner missed. It is layered ON TOP of the heuristic —
the model is shown the heuristic verdict and told to change it only where the
text plainly disagrees — and every merge is **conservative** and **canon-guarded**
(a refined speaker / POV character absent from ``known_entities`` is dropped, the
§10 no-invent rule).

Crucially this is pure-mergeable: :func:`merge_comprehension` takes a heuristic
beat and a parsed :class:`BeatComprehension` and returns the merged beat with NO
network, so the merge policy is unit-testable on its own. The model call itself
lives behind the Adapter's :class:`~app.agents.base.BaseAgent` runtime.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from app.agents.contracts import (
    Beat,
    DialogueLine,
    DiscourseMode,
    LiteraryDevice,
    NarrativePerson,
    SceneTempo,
)


class BeatComprehension(BaseModel):
    """The LLM's refined per-beat comprehension (the §10 refinement contract).

    Every field is optional so a partial reply still merges; unknown enum values
    are coerced back to the heuristic value by :func:`merge_comprehension`.
    """

    model_config = ConfigDict(extra="ignore")

    pov: str | None = None
    pov_character: str | None = None
    unreliable: bool | None = None
    discourse: str | None = None
    tempo: str | None = None
    dialogue: list[DialogueLine] | None = None
    devices: list[LiteraryDevice] | None = None


def _coerce_enum(raw: str | None, enum: type, fallback: object) -> object:
    """Coerce a model string into an enum member, falling back on any miss."""
    if raw is None:
        return fallback
    try:
        return enum(raw)
    except ValueError:
        return fallback


def merge_comprehension(
    beat: Beat,
    refined: BeatComprehension,
    *,
    known_entities: Mapping[str, str] | set[str] | None = None,
) -> Beat:
    """Conservatively merge an LLM refinement onto a heuristic beat (pure).

    Policy:
      * scalar fields (pov / discourse / tempo / unreliable) overwrite only when
        the model supplied a parseable value; an unknown enum keeps the heuristic;
      * ``pov_character`` and dialogue speakers are canon-filtered — a name not in
        ``known_entities`` is dropped (no invented entities, §10);
      * ``dialogue`` / ``devices`` overwrite wholesale only when the model supplied
        a non-empty list (so a model that omits them keeps the heuristic result).
    """
    accepted = _accept(known_entities)
    update: dict[str, object] = {}

    pov = _coerce_enum(refined.pov, NarrativePerson, beat.pov)
    update["pov"] = pov

    if refined.discourse is not None:
        update["discourse"] = _coerce_enum(refined.discourse, DiscourseMode, beat.discourse)
    if refined.tempo is not None:
        update["tempo"] = _coerce_enum(refined.tempo, SceneTempo, beat.tempo)
    if refined.unreliable is not None:
        update["unreliable"] = bool(refined.unreliable)

    # POV character: keep only an in-canon name; clear when third-person omniscient.
    if refined.pov_character is not None:
        name = refined.pov_character.strip()
        update["pov_character"] = name if _ok(name, accepted) else None
    if pov is NarrativePerson.THIRD_OMNISCIENT:
        update["pov_character"] = None

    if refined.dialogue:
        update["dialogue"] = [_filter_line(line, accepted) for line in refined.dialogue]
    if refined.devices:
        update["devices"] = list(refined.devices)

    return beat.model_copy(update=update)


def _filter_line(line: DialogueLine, accepted: set[str] | None) -> DialogueLine:
    if line.speaker and not _ok(line.speaker, accepted):
        return line.model_copy(update={"speaker": "", "inferred": True})
    return line


def _accept(known: Mapping[str, str] | set[str] | None) -> set[str] | None:
    if known is None:
        return None
    if isinstance(known, Mapping):
        return set(known) | {k.title() for k in known} | {k.lower() for k in known}
    return set(known) | {n.lower() for n in known} | {n.title() for n in known}


def _ok(name: str, accepted: set[str] | None) -> bool:
    if accepted is None:
        return True
    return bool({name, name.lower(), name.title()} & accepted)


__all__ = ["BeatComprehension", "merge_comprehension"]
