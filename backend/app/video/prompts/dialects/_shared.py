"""Shared helpers for the free-text / prose dialects.

The non-Wan dialects all assemble the same canonical fields into priority-ordered
clauses; they differ in camera vocabulary, whether they emit a structured camera
clause vs. a prose sentence, and how style/quality tokens are framed. These
helpers factor out the common assembly so each dialect is a small declaration.

Pure; no I/O.
"""

from __future__ import annotations

from ..canonical import ShotDescription


def dedupe(terms: list[str]) -> list[str]:
    """De-duplicate, case-insensitively, preserving first-seen order and casing."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        cleaned = term.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def subject_action_clause(shot: ShotDescription) -> str:
    """The lead clause: subject + action — the highest-priority content.

    Joined naturally: "<subject> <action>" when both exist, else whichever is
    present. This is the clause :func:`app.video.prompts.compress.fit_clauses`
    never drops (it is first), so a shot always renders *something* concrete.
    """
    subject = shot.subject.strip()
    action = shot.action.strip()
    if subject and action:
        return f"{subject} {action}"
    return subject or action


def negative_terms(shot: ShotDescription) -> list[str]:
    """The de-duplicated negative cues for a shot (shared across dialects)."""
    return dedupe(list(shot.negative_cues))


__all__ = ["dedupe", "negative_terms", "subject_action_clause"]
