"""The render-shot aggregate — the §9.7 per-shot state machine as an event stream.

The §9.7 diagram (kinora.md §9.7) is the canonical lifecycle of a single shot:

    Planned -> Keyframed -> Promoted -> CacheCheck
      CacheCheck -> Accepted (cache hit) | Rendering (cache miss; reserve budget)
      Rendering -> QA | Degraded
      QA -> Accepted | Repair
      Repair -> Rendering (retry <= 2) | Conflict (§7.2) | Degraded (retries exhausted)
      Conflict -> Rendering (honor/evolve) | Accepted | Degraded
      Accepted, Degraded -> terminal

This aggregate is the **write-side authority** for that lifecycle: instead of
mutating a status column, every transition is a domain event, and the legal edges
are enforced by reusing the exact :data:`app.render.states.ALLOWED_TRANSITIONS`
table — so the event-sourced model and the existing in-pipeline
:class:`~app.render.states.ShotStateMachine` can never drift. The retry budget
(``<= 2`` repairs, §9.7) is an aggregate invariant: a third repair must route to
``Degraded`` (or ``Conflict``), never back to ``Rendering``.

Decision methods are pure and named for the transition; each validates the edge
(and the retry cap) then emits the corresponding event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.db.models.enums import ShotStatus
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation
from app.eventsourcing.domain.events import DomainEvent, register_events
from app.eventsourcing.domain.identifiers import StreamCategory
from app.eventsourcing.domain.snapshotting import as_bool, as_float, as_int, as_str
from app.render.states import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    RenderState,
    is_allowed,
    to_status,
)

#: §9.7: "Repair --> Rendering: regen (retry <= 2)". The third failure may not
#: re-enter Rendering — it must degrade or raise a conflict.
MAX_REPAIRS: int = 2


# --------------------------------------------------------------------------- #
# Events — one per §9.7 transition (carrying the destination state)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ShotPlanned(DomainEvent):
    """A shot entered the shot list in Phase A (§9.1) — the stream's genesis."""

    shot_id: str = ""
    book_id: str = ""
    scene_id: str = ""
    beat_id: str = ""
    shot_hash: str = ""


@dataclass(frozen=True, slots=True)
class ShotTransitioned(DomainEvent):
    """A §9.7 state transition (the general edge event).

    ``to_state`` is a :class:`RenderState` value; ``reason`` is a short tag for the
    audit trail (e.g. ``"cache_miss"``, ``"qa_fail"``, ``"retries_exhausted"``).
    """

    shot_id: str = ""
    from_state: str = ""
    to_state: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShotKeyframed(DomainEvent):
    """Speculative-zone keyframe produced (image-gen, no video-seconds; §4.4)."""

    shot_id: str = ""
    keyframe_url: str = ""


@dataclass(frozen=True, slots=True)
class ShotRendered(DomainEvent):
    """A clip + narration were produced (Rendering -> QA). Records video-seconds."""

    shot_id: str = ""
    clip_url: str = ""
    video_seconds: float = 0.0
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class ShotQAScored(DomainEvent):
    """The Critic scored the clip against canon (§9.5)."""

    shot_id: str = ""
    score: float = 0.0
    passed: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShotAccepted(DomainEvent):
    """Terminal: QA passed (or a cache hit / arbitration cleared). Last frame->canon."""

    shot_id: str = ""
    clip_url: str = ""
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class ShotDegraded(DomainEvent):
    """Terminal: retries exhausted -> Ken-Burns fallback (§12.4). Defect logged."""

    shot_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShotConflictRaised(DomainEvent):
    """A timeline contradiction routed to arbitration (§7.2)."""

    shot_id: str = ""
    contradicting_state_id: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ShotRegenRequested(DomainEvent):
    """A Director comment / canon edit asked to re-do this shot (§5.4).

    This is **not** a QA-fail repair — it re-opens an already-settled shot
    (Accepted/Degraded) or pre-empts an in-flight one, returning it to ``Promoted``
    so it re-enters the render flow. It resets the per-attempt repair count
    because the regeneration is a fresh attempt against (usually) new canon.
    """

    shot_id: str = ""
    reason: str = ""
    triggered_by: str = ""


register_events(
    ShotPlanned,
    ShotTransitioned,
    ShotKeyframed,
    ShotRendered,
    ShotQAScored,
    ShotAccepted,
    ShotDegraded,
    ShotRegenRequested,
    ShotConflictRaised,
)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RenderShotAggregate(AggregateRoot):
    """Event-sourced §9.7 render-shot.

    Reuses :data:`app.render.states.ALLOWED_TRANSITIONS` so the legal edges match
    the in-pipeline state machine exactly. Tracks the repair count to enforce the
    ``<= 2`` retry cap, and accumulates the video-seconds spent so the budget
    accounting (§11.1) has an event-sourced source of truth.
    """

    category = StreamCategory.RENDER_SHOT

    planned: bool = False
    state: RenderState = RenderState.PLANNED
    repair_count: int = 0
    qa_passed: bool | None = None
    video_seconds_spent: float = 0.0
    clip_url: str = ""
    book_id: str = ""
    shot_hash: str = ""

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self.planned = False
        self.state = RenderState.PLANNED
        self.repair_count = 0
        self.qa_passed = None
        self.video_seconds_spent = 0.0
        self.clip_url = ""
        self.book_id = ""
        self.shot_hash = ""

    # -- genesis ------------------------------------------------------------- #

    def plan(
        self,
        *,
        book_id: str,
        scene_id: str,
        beat_id: str,
        shot_hash: str,
    ) -> None:
        """Genesis: a shot enters the Phase-A shot list (§9.1)."""
        if self.planned:
            raise CommandRejected(f"shot {self.aggregate_id} already planned")
        self.emit(
            ShotPlanned(
                shot_id=self.aggregate_id,
                book_id=book_id,
                scene_id=scene_id,
                beat_id=beat_id,
                shot_hash=shot_hash,
            )
        )

    # -- §9.7 transitions ---------------------------------------------------- #

    def keyframe(self, *, keyframe_url: str) -> None:
        """Planned -> Keyframed (speculative image-gen)."""
        self._transition(RenderState.KEYFRAMED, reason="keyframed")
        self.emit(ShotKeyframed(shot_id=self.aggregate_id, keyframe_url=keyframe_url))

    def promote(self) -> None:
        """Planned/Keyframed -> Promoted (ETA < C and stable; §4.6)."""
        self._transition(RenderState.PROMOTED, reason="promoted")

    def begin_cache_check(self) -> None:
        """Promoted -> CacheCheck (shot.render starts on the shot_hash)."""
        self._transition(RenderState.CACHE_CHECK, reason="cache_check")

    def cache_hit(self, *, clip_url: str) -> None:
        """CacheCheck -> Accepted with zero video-seconds (§8.7 dedup)."""
        self._transition(RenderState.ACCEPTED, reason="cache_hit")
        self.emit(ShotAccepted(shot_id=self.aggregate_id, clip_url=clip_url, from_cache=True))

    def cache_miss(self) -> None:
        """CacheCheck -> Rendering (reserve budget)."""
        self._transition(RenderState.RENDERING, reason="cache_miss")

    def rendered(self, *, clip_url: str, video_seconds: float) -> None:
        """Rendering -> QA: the clip + narration were produced."""
        if video_seconds < 0:
            raise InvariantViolation("video_seconds must be non-negative")
        self._transition(RenderState.QA, reason="rendered")
        self.emit(
            ShotRendered(
                shot_id=self.aggregate_id,
                clip_url=clip_url,
                video_seconds=video_seconds,
                from_cache=False,
            )
        )

    def score_qa(self, *, score: float, passed: bool, reason: str = "") -> None:
        """Record the Critic's QA verdict while in QA (does not itself transition)."""
        if self.state is not RenderState.QA:
            raise InvariantViolation(f"cannot score QA from {self.state.value}; shot must be in QA")
        self.emit(
            ShotQAScored(shot_id=self.aggregate_id, score=score, passed=passed, reason=reason)
        )

    def accept_after_qa(self) -> None:
        """QA -> Accepted: all checks passed (§9.7)."""
        if self.qa_passed is False:
            raise InvariantViolation("cannot accept a shot whose QA failed")
        self._transition(RenderState.ACCEPTED, reason="qa_pass")
        self.emit(ShotAccepted(shot_id=self.aggregate_id, clip_url=self.clip_url, from_cache=False))

    def repair(self) -> None:
        """QA -> Repair: a check failed; route the regen decision."""
        self._transition(RenderState.REPAIR, reason="qa_fail")

    def regen(self) -> None:
        """Repair -> Rendering: regenerate, enforcing the §9.7 ``<= 2`` retry cap.

        The repair count is incremented *here* (the moment a regen is committed).
        A third regen attempt is an invariant violation — the caller must degrade
        or raise a conflict instead.
        """
        if self.state is not RenderState.REPAIR:
            raise InvariantViolation(f"regen requires REPAIR state, not {self.state.value}")
        if self.repair_count >= MAX_REPAIRS:
            raise InvariantViolation(
                f"retry cap reached ({self.repair_count}/{MAX_REPAIRS}); "
                "shot must degrade or raise a conflict"
            )
        self._transition(RenderState.RENDERING, reason=f"regen#{self.repair_count + 1}")

    def raise_conflict(self, *, contradicting_state_id: str = "", detail: str = "") -> None:
        """Repair -> Conflict: a timeline contradiction routes to §7.2 arbitration."""
        self._transition(RenderState.CONFLICT, reason="conflict")
        self.emit(
            ShotConflictRaised(
                shot_id=self.aggregate_id,
                contradicting_state_id=contradicting_state_id,
                detail=detail,
            )
        )

    def resolve_conflict_regen(self) -> None:
        """Conflict -> Rendering: arbitration chose honor/evolve -> regenerate (§7.2)."""
        self._transition(RenderState.RENDERING, reason="conflict_resolved")

    def resolve_conflict_accept(self) -> None:
        """Conflict -> Accepted: arbitration cleared the contradiction (§7.2)."""
        self._transition(RenderState.ACCEPTED, reason="conflict_cleared")
        self.emit(ShotAccepted(shot_id=self.aggregate_id, clip_url=self.clip_url, from_cache=False))

    def degrade(self, *, reason: str = "retries_exhausted") -> None:
        """-> Degraded: ride the Ken-Burns ladder (§12.4). Valid from Rendering/Repair/Conflict."""
        self._transition(RenderState.DEGRADED, reason=reason)
        self.emit(ShotDegraded(shot_id=self.aggregate_id, reason=reason))

    def request_regen(self, *, reason: str = "", triggered_by: str = "") -> bool:
        """§5.4 re-do: re-open a settled/in-flight shot back to ``Promoted``.

        This is the surgical-regeneration entry the Director comment / canon-edit
        sagas use. Unlike :meth:`regen` (the §9.7 *intra-attempt* QA-fail retry,
        capped at ``<= 2``), this re-opens a shot that has **left** the QA loop —
        typically an ``Accepted`` shot the reader edited, or a ``Degraded`` shot a
        canon fix should now retry — and resets the per-attempt repair count so the
        fresh render against new canon gets its full retry budget again.

        It deliberately does *not* go through the §9.7 edge table: a re-do is a
        cross-cutting lifecycle event, not one of the diagram's transitions. It is
        a no-op (returns ``False``) when the shot is already back in the render
        flow (Planned/Keyframed/Promoted/CacheCheck), so a duplicate trigger is safe.
        """
        if not self.planned:
            raise CommandRejected(f"shot {self.aggregate_id} has not been planned")
        # Already re-openable / mid-flow -> nothing to re-open.
        if self.state in {
            RenderState.PLANNED,
            RenderState.KEYFRAMED,
            RenderState.PROMOTED,
            RenderState.CACHE_CHECK,
        }:
            return False
        self.emit(
            ShotRegenRequested(shot_id=self.aggregate_id, reason=reason, triggered_by=triggered_by)
        )
        return True

    # -- core edge primitive ------------------------------------------------- #

    def _transition(self, dst: RenderState, *, reason: str) -> None:
        if not self.planned:
            raise CommandRejected(f"shot {self.aggregate_id} has not been planned")
        if self.state in TERMINAL_STATES:
            raise InvariantViolation(
                f"shot is terminal ({self.state.value}); no further transitions"
            )
        if not is_allowed(self.state, dst):
            raise InvariantViolation(f"illegal §9.7 transition {self.state.value} -> {dst.value}")
        self.emit(
            ShotTransitioned(
                shot_id=self.aggregate_id,
                from_state=self.state.value,
                to_state=dst.value,
                reason=reason,
            )
        )

    # -- projections --------------------------------------------------------- #

    @property
    def status(self) -> ShotStatus:
        """The coarse persisted status this shot projects onto (§9.7 ``to_status``)."""
        return to_status(self.state)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    # -- fold ---------------------------------------------------------------- #

    def apply(self, event: DomainEvent) -> None:
        if isinstance(event, ShotPlanned):
            self.planned = True
            self.state = RenderState.PLANNED
            self.book_id = event.book_id
            self.shot_hash = event.shot_hash
        elif isinstance(event, ShotTransitioned):
            dst = RenderState(event.to_state)
            # Count a repair the moment a regen edge (Repair -> Rendering) commits.
            if RenderState(event.from_state) is RenderState.REPAIR and dst is RenderState.RENDERING:
                self.repair_count += 1
            self.state = dst
        elif isinstance(event, ShotRendered):
            self.clip_url = event.clip_url
            if not event.from_cache:
                self.video_seconds_spent += event.video_seconds
        elif isinstance(event, ShotQAScored):
            self.qa_passed = event.passed
        elif isinstance(event, ShotAccepted):
            if event.clip_url:
                self.clip_url = event.clip_url
        elif isinstance(event, ShotRegenRequested):
            # §5.4 re-do: re-open to Promoted with a fresh attempt budget.
            self.state = RenderState.PROMOTED
            self.repair_count = 0
            self.qa_passed = None
        # ShotKeyframed / ShotDegraded / ShotConflictRaised carry no state we fold
        # beyond the transition they accompany; unknown events ignored.

    # -- snapshotting -------------------------------------------------------- #

    def snapshot_state(self) -> dict[str, object]:
        return {
            "planned": self.planned,
            "state": self.state.value,
            "repair_count": self.repair_count,
            "qa_passed": self.qa_passed,
            "video_seconds_spent": self.video_seconds_spent,
            "clip_url": self.clip_url,
            "book_id": self.book_id,
            "shot_hash": self.shot_hash,
        }

    def restore_state(self, state: Mapping[str, object], *, version: int) -> None:
        self.planned = as_bool(state.get("planned"))
        self.state = RenderState(as_str(state.get("state"), RenderState.PLANNED.value))
        self.repair_count = as_int(state.get("repair_count"))
        raw_qa = state.get("qa_passed")
        self.qa_passed = raw_qa if isinstance(raw_qa, bool) else None
        self.video_seconds_spent = as_float(state.get("video_seconds_spent"))
        self.clip_url = as_str(state.get("clip_url"))
        self.book_id = as_str(state.get("book_id"))
        self.shot_hash = as_str(state.get("shot_hash"))
        self.version = version
        self._committed_version = version


# Default-construction helpers asserting the table did not silently change shape.
assert ALLOWED_TRANSITIONS[RenderState.REPAIR]  # noqa: S101 - import-time invariant check


__all__ = [
    "MAX_REPAIRS",
    "RenderShotAggregate",
    "ShotAccepted",
    "ShotConflictRaised",
    "ShotDegraded",
    "ShotKeyframed",
    "ShotPlanned",
    "ShotQAScored",
    "ShotRegenRequested",
    "ShotRendered",
    "ShotTransitioned",
]
