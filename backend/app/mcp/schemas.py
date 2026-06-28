"""Typed input/output models for every MCP tool (kinora.md §8.3).

Inputs are deliberately *flat* (scalars, lists, and plain JSON objects) so the
JSON Schemas the MCP server advertises validate cleanly and read well as Qwen
function-call parameters (§14). Outputs reuse the memory-layer contracts
(``CanonSlice``, ``CanonEntitySlice``, ``ShotSpec``, ``EpisodicShotRef``,
``PreferencePrior(s)``) so the tool surface and the agents speak the same types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.memory.contracts import (
    AuditChain,
    BitemporalFact,
    BranchDiff,
    BranchInfo,
    CanonReadView,
    FactHistory,
    MergeResult,
)
from app.memory.interfaces import CanonEntitySlice, CanonSlice, EpisodicShotRef, ShotSpec
from app.memory.prefs_service import PreferencePrior, PreferencePriors

__all__ = [
    "AuditChain",
    "BitemporalFact",
    "BranchDiff",
    "BranchInfo",
    "BudgetRemainingInput",
    "BudgetRemainingOutput",
    "BudgetReserveInput",
    "BudgetReserveOutput",
    "CanonAssertFactInput",
    "CanonAssertStateInput",
    "CanonAssertStateOutput",
    "CanonAuditInput",
    "CanonCompactInput",
    "CanonCompactOutput",
    "CanonCorrectFactInput",
    "CanonDiffInput",
    "CanonEntitySlice",
    "CanonFactHistoryInput",
    "CanonFactsAsOfInput",
    "CanonFactsAsOfOutput",
    "CanonForkInput",
    "CanonGetEntityInput",
    "CanonGetEntityOutput",
    "CanonMergeInput",
    "CanonQueryInput",
    "CanonReadView",
    "CanonRetireFactInput",
    "CanonRetireStateInput",
    "CanonRetireStateOutput",
    "CanonSlice",
    "CanonUpsertEntityInput",
    "CanonUpsertEntityOutput",
    "CanonVaultInput",
    "CanonVaultOutput",
    "CanonViewInput",
    "EpisodicLogInput",
    "EpisodicLogOutput",
    "EpisodicSearchInput",
    "EpisodicSearchOutput",
    "EpisodicShotRef",
    "FactHistory",
    "MergeResult",
    "PreferencePrior",
    "PreferencePriors",
    "PrefsGetInput",
    "PrefsUpsertInput",
    "ShotPlanInput",
    "ShotPlanOutput",
    "ShotRenderInput",
    "ShotRenderOutput",
    "ShotResultInput",
    "ShotResultOutput",
    "ShotSpec",
    "ShotStatusInput",
    "ShotStatusOutput",
]


# --- canon.query -------------------------------------------------------------


class CanonQueryInput(BaseModel):
    """Args for ``canon.query`` — the retrieval policy for one beat (§8.4)."""

    book_id: str
    beat_id: str
    kinds: list[str] | None = Field(
        default=None,
        description="Optional entity-kind filter (character/location/prop/style).",
    )
    episodic_k: int = Field(default=3, description="How many prior similar shots to recall.")


# --- canon.get_entity --------------------------------------------------------


class CanonGetEntityInput(BaseModel):
    """Args for ``canon.get_entity`` — a time-travel read (§8.3)."""

    book_id: str
    entity_key: str
    at_beat: int | None = Field(default=None, description="Beat ordinal; latest when omitted.")


class CanonGetEntityOutput(BaseModel):
    """Result of ``canon.get_entity``."""

    found: bool
    entity: CanonEntitySlice | None = None


# --- canon.upsert_entity -----------------------------------------------------


class CanonUpsertEntityInput(BaseModel):
    """Args for ``canon.upsert_entity`` — a Continuity Supervisor write (§8.1)."""

    book_id: str
    entity_key: str
    type: str = Field(description="character | location | prop | style")
    name: str
    valid_from_beat: int
    aliases: list[str] | None = None
    description: str | None = None
    appearance: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    style_tokens: dict[str, Any] | None = None
    first_appearance: dict[str, Any] | None = None
    entity_id: str | None = None


class CanonUpsertEntityOutput(BaseModel):
    """Result of ``canon.upsert_entity`` — the new version number."""

    entity_key: str
    version: int


# --- canon.assert_state / retire_state --------------------------------------


class CanonAssertStateInput(BaseModel):
    """Args for ``canon.assert_state`` — add a versioned fact (§8.1)."""

    book_id: str
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    source_span: dict[str, Any] | None = None
    state_id: str | None = None


class CanonAssertStateOutput(BaseModel):
    """Result of ``canon.assert_state``."""

    state_id: str


class CanonRetireStateInput(BaseModel):
    """Args for ``canon.retire_state`` — forgetting via interval close (§8.5)."""

    state_id: str
    valid_to_beat: int


class CanonRetireStateOutput(BaseModel):
    """Result of ``canon.retire_state``."""

    state_id: str
    valid_to_beat: int
    retired: bool = True


# --- shot.plan ---------------------------------------------------------------


class ShotPlanInput(BaseModel):
    """Args for ``shot.plan`` — the Adapter's scene decomposition (§8.3)."""

    scene_id: str


class ShotPlanOutput(BaseModel):
    """Result of ``shot.plan`` — the ordered shot list for a scene."""

    scene_id: str
    shots: list[ShotSpec] = Field(default_factory=list)


# --- shot.render -------------------------------------------------------------


class ShotRenderInput(BaseModel):
    """Args for ``shot.render`` — cache-first, budget-gated enqueue (§8.7, §11)."""

    book_id: str
    beat_id: str
    scene_id: str | None = None
    shot_id: str | None = None
    render_mode: str = "reference_to_video"
    prompt: str = ""
    negative_prompt: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    camera: dict[str, Any] | None = None
    seed: int = 0
    target_duration_s: float = 5.0
    canon_version_at_render: int = 1
    end_frame_ref: str | None = None
    priority: str = "committed"
    session_id: str | None = None
    cancel_token: str | None = None


class ShotRenderOutput(BaseModel):
    """Result of ``shot.render``.

    ``status`` is ``cache_hit`` (0 video-seconds, §8.7) or ``enqueued`` (a render
    job was enqueued). ``shot.render`` does **not** reserve budget — the
    RenderPipeline owns the single reserve → commit/release lifecycle, so the
    budget can never be double-counted or leaked. ``video_seconds`` on an
    ``enqueued`` result is the *estimate* the pipeline will spend.
    """

    status: str
    cached: bool
    shot_hash: str
    reference_set_hash: str
    clip_url: str | None = None
    last_frame_url: str | None = None
    video_seconds: float = 0.0
    reservation_id: str | None = None
    job_id: str | None = None
    remaining_video_s: float | None = None
    reason: str | None = None


# --- shot.status / shot.result ----------------------------------------------


class ShotStatusInput(BaseModel):
    """Args for ``shot.status`` — poll a render job."""

    job_id: str


class ShotStatusOutput(BaseModel):
    """Result of ``shot.status``."""

    found: bool
    job_id: str
    status: str | None = None
    attempts: int | None = None
    provider_task_id: str | None = None
    error: str | None = None
    shot_id: str | None = None
    shot_hash: str | None = None


class ShotResultInput(BaseModel):
    """Args for ``shot.result`` — fetch a finished shot's output."""

    shot_id: str


class ShotResultOutput(BaseModel):
    """Result of ``shot.result``."""

    found: bool
    shot_id: str
    status: str | None = None
    output: dict[str, Any] | None = None
    narration: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    duration_s: float | None = None
    clip_url: str | None = None
    last_frame_url: str | None = None


# --- episodic.search / log ---------------------------------------------------


class EpisodicSearchInput(BaseModel):
    """Args for ``episodic.search`` — nearest prior accepted shots (§8.2)."""

    book_id: str
    described_visuals_text: str | None = None
    query_embedding: list[float] | None = None
    query_image_key: str | None = None
    k: int = 5
    filters: dict[str, Any] | None = None


class EpisodicSearchOutput(BaseModel):
    """Result of ``episodic.search``."""

    shots: list[EpisodicShotRef] = Field(default_factory=list)


class EpisodicLogInput(BaseModel):
    """Args for ``episodic.log`` — persist a shot + QA + embedding (§8.2)."""

    book_id: str
    status: str = "accepted"
    shot_id: str | None = None
    beat_id: str | None = None
    scene_id: str | None = None
    source_span: dict[str, Any] | None = None
    render_mode: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    reference_set_hash: str | None = None
    reference_image_ids: list[str] | None = None
    duration_s: float | None = None
    output: dict[str, Any] | None = None
    narration: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    canon_version_at_render: int | None = None
    shot_hash: str | None = None
    last_frame_key: str | None = None
    keyframe_key: str | None = None
    described_visuals_text: str | None = None


class EpisodicLogOutput(BaseModel):
    """Result of ``episodic.log``."""

    shot_id: str
    status: str


# --- budget.reserve / remaining ---------------------------------------------


class BudgetReserveInput(BaseModel):
    """Args for ``budget.reserve`` — earmark video-seconds (§11)."""

    video_seconds: float
    session_id: str | None = None
    scene_id: str | None = None
    book_id: str | None = None


class BudgetReserveOutput(BaseModel):
    """Result of ``budget.reserve`` (``reserved=False`` when a cap blocked it)."""

    reserved: bool
    video_seconds: float
    remaining_video_s: float
    reservation_id: str | None = None
    reason: str | None = None
    scope: str | None = None


class BudgetRemainingInput(BaseModel):
    """Args for ``budget.remaining`` (no parameters)."""


class BudgetRemainingOutput(BaseModel):
    """Result of ``budget.remaining`` — the guardrail snapshot (§11)."""

    remaining_video_s: float
    ceiling_video_s: float
    is_low: bool
    can_render_live: bool


# --- prefs.get / upsert ------------------------------------------------------


class PrefsGetInput(BaseModel):
    """Args for ``prefs.get`` — aggregated priors for a scope (§8.6)."""

    user_id: str | None = None
    book_id: str | None = None


class PrefsUpsertInput(BaseModel):
    """Args for ``prefs.upsert`` — a Director-edit preference signal (§8.6)."""

    kind: str
    value: dict[str, Any]
    user_id: str | None = None
    book_id: str | None = None
    weight_delta: float = 1.0


# --- bitemporal canon engine (§8: VALID-time AND TRANSACTION-time) -----------


class CanonAssertFactInput(BaseModel):
    """Args for ``canon.assert_fact`` — a bitemporal fact assert (audited + CRDT-stamped)."""

    book_id: str
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    branch: str = "main"
    fact_key: str | None = None
    actor_id: str = "system"
    source_span: dict[str, Any] | None = None


class CanonCorrectFactInput(BaseModel):
    """Args for ``canon.correct_fact`` — change a belief (close tx, insert successor)."""

    book_id: str
    fact_key: str
    new_object: str
    branch: str = "main"
    new_valid_from_beat: int | None = None
    actor_id: str = "system"
    source_span: dict[str, Any] | None = None


class CanonRetireFactInput(BaseModel):
    """Args for ``canon.retire_fact`` — §8.5 forgetting on the bitemporal store."""

    book_id: str
    fact_key: str
    valid_to_beat: int
    branch: str = "main"
    actor_id: str = "system"


class CanonFactsAsOfInput(BaseModel):
    """Args for ``canon.facts_as_of`` — the 4-D time-travel read."""

    book_id: str
    beat: int
    as_of_tx: datetime | None = Field(
        default=None, description="Transaction instant (UTC); current belief when omitted."
    )
    branch: str = "main"
    subject_entity_key: str | None = None


class CanonFactsAsOfOutput(BaseModel):
    """Result of ``canon.facts_as_of`` — the active facts at the coordinate."""

    facts: list[BitemporalFact] = Field(default_factory=list)


class CanonFactHistoryInput(BaseModel):
    """Args for ``canon.fact_history`` — every past belief of one logical fact."""

    book_id: str
    fact_key: str
    branch: str = "main"


class CanonForkInput(BaseModel):
    """Args for ``canon.fork`` — create an editing branch off a base coordinate."""

    book_id: str
    name: str
    base_beat: int | None = None
    base_tx: datetime | None = None
    parent: str = "main"
    actor_id: str = "system"
    note: str | None = None


class CanonDiffInput(BaseModel):
    """Args for ``canon.diff`` — the structural difference between two branches."""

    book_id: str
    branch_a: str
    branch_b: str


class CanonMergeInput(BaseModel):
    """Args for ``canon.merge`` — three-way CRDT merge of ``source`` into ``target``."""

    book_id: str
    source: str
    target: str = "main"
    actor_id: str = "system"


class CanonAuditInput(BaseModel):
    """Args for ``canon.audit`` — replay (a tail of) the hash-chained audit log."""

    book_id: str
    limit: int | None = Field(default=None, description="Tail size; whole log when omitted.")


class CanonViewInput(BaseModel):
    """Args for ``canon.view`` — the inspectable read contract for the frontend."""

    book_id: str
    beat: int | None = Field(default=None, description="Beat ordinal; latest when omitted.")
    as_of_tx: datetime | None = None
    branch: str = "main"
    audit_tail: int = 20


class CanonCompactInput(BaseModel):
    """Args for ``canon.compact`` — prune superseded tx-history beyond a horizon (§8.7)."""

    book_id: str
    branch: str = "main"
    horizon_days: int = 30
    dry_run: bool = Field(
        default=True, description="Plan only (default); set False to actually prune."
    )


class CanonCompactOutput(BaseModel):
    """Result of ``canon.compact`` — what was (or would be) pruned."""

    book_id: str
    branch: str
    dry_run: bool
    prunable: int
    pruned: int
    facts_touched: int


class CanonVaultInput(BaseModel):
    """Args for ``canon.vault`` — render the bitemporal canon to inspectable markdown."""

    book_id: str
    branch: str = "main"
    beat: int | None = None
    history_for: list[str] | None = Field(
        default=None, description="fact_keys to include full tx-history for (all when omitted)."
    )
    audit_tail: int = 50


class CanonVaultOutput(BaseModel):
    """Result of ``canon.vault`` — the rendered markdown sections + the joined document."""

    book_id: str
    branch: str
    markdown: str
    sections: dict[str, str] = Field(default_factory=dict)
