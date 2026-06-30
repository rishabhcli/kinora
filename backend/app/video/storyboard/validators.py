"""Storyboard validators: the invariants a well-formed storyboard must satisfy.

The planner's deterministic assembly produces a storyboard; these validators are
the safety net that catches a planted (or regressed) defect before the storyboard
feeds the dialect layer. Each validator is a pure function returning a list of
:class:`ValidationIssue`; :func:`validate_storyboard` runs them all and returns the
combined list. The engine uses the issues to drive a re-plan/refine pass.

Checked invariants:

- **No orphan entities.** Every entity a shot claims to show must be a member of
  the passage's canon context (the §10 no-invent guardrail) and of the parent
  beat's entities. A shot may not reference an entity its beat never names.
- **Duration budget.** The realised total screen-time must be within the budget's
  ``tolerance_s`` of ``target_total_s``, and every shot must sit inside the
  ``[min_shot_s, max_shot_s]`` band.
- **Shot-count budget.** The storyboard must not exceed ``max_shots`` (the §-style
  ceiling) nor fall below ``min_shots``.
- **Narration coverage.** Every shot must carry non-empty narration *and* the
  union of the shots' source spans must cover every beat's span with no gap —
  every word the reader sees has a shot.
- **Continuity integrity.** A continuity link that names a ``from_shot_id`` must
  point at the immediately preceding shot; the first shot must be a scene start.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .models import ContinuityKind, PassageBeat, Storyboard


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    """One validation finding: a code, a severity, and a human message."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: IssueSeverity = IssueSeverity.ERROR
    message: str
    shot_id: str | None = None
    beat_id: str | None = None


def validate_storyboard(
    storyboard: Storyboard,
    beats: list[PassageBeat],
    *,
    allowed_entities: set[str] | None = None,
) -> list[ValidationIssue]:
    """Run every validator and return the combined issue list (errors + warnings).

    ``allowed_entities`` is the passage's canon context entity set; when omitted it
    defaults to the union of the beats' entities (so the orphan check still runs).
    """
    beat_by_id = {b.beat_id: b for b in beats}
    allowed = allowed_entities if allowed_entities is not None else _beats_entity_union(beats)

    issues: list[ValidationIssue] = []
    issues += _check_orphan_entities(storyboard, beat_by_id, allowed)
    issues += _check_durations(storyboard)
    issues += _check_shot_count(storyboard)
    issues += _check_narration_coverage(storyboard, beats)
    issues += _check_continuity(storyboard)
    return issues


def has_errors(issues: list[ValidationIssue]) -> bool:
    return any(i.severity is IssueSeverity.ERROR for i in issues)


def _beats_entity_union(beats: list[PassageBeat]) -> set[str]:
    union: set[str] = set()
    for b in beats:
        union.update(b.entities)
    return union


def _check_orphan_entities(
    storyboard: Storyboard,
    beat_by_id: dict[str, PassageBeat],
    allowed: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for shot in storyboard.shots:
        beat = beat_by_id.get(shot.beat_id)
        beat_entities = set(beat.entities) if beat else set()
        # Every shown entity (and every reference entity) must be canon-known and
        # named by the parent beat.
        claimed = set(shot.entities) | set(shot.intent.reference_entities)
        if shot.intent.pov_character:
            claimed.add(shot.intent.pov_character)
        for entity in sorted(claimed):
            if entity not in allowed:
                issues.append(
                    ValidationIssue(
                        code="orphan_entity_not_in_canon",
                        message=f"shot {shot.shot_id} references {entity!r} not in canon context",
                        shot_id=shot.shot_id,
                        beat_id=shot.beat_id,
                    )
                )
            elif beat is not None and entity not in beat_entities:
                issues.append(
                    ValidationIssue(
                        code="orphan_entity_not_in_beat",
                        message=(
                            f"shot {shot.shot_id} references {entity!r} not named by "
                            f"beat {shot.beat_id}"
                        ),
                        shot_id=shot.shot_id,
                        beat_id=shot.beat_id,
                    )
                )
    return issues


def _check_durations(storyboard: Storyboard) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    budget = storyboard.budget
    for shot in storyboard.shots:
        # A small epsilon absorbs 0.1s rounding at the band edges.
        if shot.duration_s < budget.min_shot_s - 0.05 or shot.duration_s > budget.max_shot_s + 0.05:
            issues.append(
                ValidationIssue(
                    code="shot_duration_out_of_band",
                    message=(
                        f"shot {shot.shot_id} duration {shot.duration_s}s outside "
                        f"[{budget.min_shot_s}, {budget.max_shot_s}]"
                    ),
                    shot_id=shot.shot_id,
                    beat_id=shot.beat_id,
                )
            )
    drift = abs(storyboard.total_duration_s - budget.target_total_s)
    if drift > budget.tolerance_s + 0.05:
        issues.append(
            ValidationIssue(
                code="total_duration_off_budget",
                message=(
                    f"total {storyboard.total_duration_s}s drifts {round(drift, 2)}s from "
                    f"target {budget.target_total_s}s (tolerance {budget.tolerance_s}s)"
                ),
            )
        )
    return issues


def _check_shot_count(storyboard: Storyboard) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    budget = storyboard.budget
    if storyboard.shot_count > budget.max_shots:
        issues.append(
            ValidationIssue(
                code="shot_count_over_budget",
                message=f"{storyboard.shot_count} shots exceeds max_shots {budget.max_shots}",
            )
        )
    if storyboard.shot_count < budget.min_shots:
        issues.append(
            ValidationIssue(
                code="shot_count_under_minimum",
                message=f"{storyboard.shot_count} shots below min_shots {budget.min_shots}",
            )
        )
    return issues


def _check_narration_coverage(
    storyboard: Storyboard, beats: list[PassageBeat]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    # Every shot must carry narration text.
    for shot in storyboard.shots:
        if not shot.narration.strip():
            issues.append(
                ValidationIssue(
                    code="shot_missing_narration",
                    message=f"shot {shot.shot_id} has empty narration",
                    shot_id=shot.shot_id,
                    beat_id=shot.beat_id,
                )
            )

    # Every beat must be covered by at least one shot whose span overlaps it, and
    # the shots' spans must leave no gap inside a beat's word range.
    shots_by_beat: dict[str, list[tuple[int, int]]] = {}
    for shot in storyboard.shots:
        shots_by_beat.setdefault(shot.beat_id, []).append(shot.source_span.word_range)

    for beat in beats:
        spans = sorted(shots_by_beat.get(beat.beat_id, []))
        if not spans:
            issues.append(
                ValidationIssue(
                    code="beat_uncovered",
                    message=f"beat {beat.beat_id} has no shot covering it",
                    beat_id=beat.beat_id,
                )
            )
            continue
        gap = _coverage_gap(beat.word_range, spans)
        if gap is not None:
            issues.append(
                ValidationIssue(
                    code="beat_coverage_gap",
                    message=(
                        f"beat {beat.beat_id} span {beat.word_range} has an uncovered "
                        f"gap at {gap}"
                    ),
                    beat_id=beat.beat_id,
                )
            )
    return issues


def _coverage_gap(
    beat_range: tuple[int, int], spans: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """Return the first uncovered ``[lo, hi)`` gap inside ``beat_range``, else None.

    Zero-width beat spans are treated as covered by any shot of that beat (a beat
    with no resolved word range still earns coverage by presence).
    """
    lo, hi = beat_range
    if hi <= lo:
        return None  # unknown/zero-width span — presence is enough
    cursor = lo
    for s_lo, s_hi in spans:
        if s_lo > cursor:
            return (cursor, s_lo)  # an uncovered gap before this span starts
        cursor = max(cursor, s_hi)
        if cursor >= hi:
            return None
    if cursor < hi:
        return (cursor, hi)
    return None


def _check_continuity(storyboard: Storyboard) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    shots = storyboard.shots
    for i, shot in enumerate(shots):
        link = shot.continuity
        if i == 0:
            if link.kind is not ContinuityKind.SCENE_START:
                issues.append(
                    ValidationIssue(
                        code="first_shot_not_scene_start",
                        message=f"first shot {shot.shot_id} is {link.kind} not scene_start",
                        shot_id=shot.shot_id,
                    )
                )
            continue
        if link.kind is ContinuityKind.SCENE_START:
            issues.append(
                ValidationIssue(
                    code="mid_shot_scene_start",
                    message=f"shot {shot.shot_id} at index {i} is a scene_start mid-storyboard",
                    shot_id=shot.shot_id,
                )
            )
        if link.from_shot_id is not None and link.from_shot_id != shots[i - 1].shot_id:
            issues.append(
                ValidationIssue(
                    code="continuity_anchor_not_predecessor",
                    message=(
                        f"shot {shot.shot_id} anchors on {link.from_shot_id} which is not its "
                        f"predecessor {shots[i - 1].shot_id}"
                    ),
                    shot_id=shot.shot_id,
                )
            )
    return issues


__all__ = [
    "IssueSeverity",
    "ValidationIssue",
    "has_errors",
    "validate_storyboard",
]
