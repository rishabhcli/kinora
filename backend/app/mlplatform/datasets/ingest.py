"""Normalize raw traces into immutable :class:`TraceExample` rows.

The ingest stage is the boundary between the messy upstream (run traces with
free-form input dicts, optional QA records, optional director edits) and the
clean, typed, immutable training example the rest of the pipeline trusts. It is
pure and deterministic: same :class:`RawTrace` in → same
:class:`TraceExample` out (the example id is a stable hash, so re-ingesting a
trace is idempotent).

Responsibilities:

* **Role mapping.** A trace's ``prompt_key`` (e.g. ``adapter@v3``,
  ``critic.qa``) maps to an :class:`AgentRole` via a small, explicit table — so
  the pipeline can stratify per role without importing the agents.
* **Task inference.** A trace with a QA verdict and/or director edits becomes a
  ``PREFERENCE``/``REWARD`` example (it carries a learning *signal*); a clean,
  QA-passed trace becomes an ``SFT`` example; otherwise ``SFT`` with no filter.
  The caller can force a task.
* **Signal projection.** A raw QA dict (Critic §9.5 projection) → :class:`QAVerdict`;
  raw director-edit dicts (§5.4) → :class:`DirectorEdit`; a scalar reward is
  derived from the QA verdict + edit count when not supplied (the alignment
  facet's default reward; it can override).
* **Drop policy.** Errored traces and cache hits are dropped by default (a cache
  hit is a replay, not a fresh behaviour; an errored call has no usable output) —
  configurable via :class:`IngestConfig`.

No model calls, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.mlplatform.datasets.contracts import (
    AgentRole,
    DirectorEdit,
    QAVerdict,
    RawTrace,
    Split,
    TaskType,
    TraceExample,
    stable_hash,
)
from app.mlplatform.datasets.errors import IngestError

# --------------------------------------------------------------------------- #
# Role mapping — prompt_key → crew role (explicit, no agents import)
# --------------------------------------------------------------------------- #

#: Substrings that identify each crew role within a ``prompt_key``. Checked in
#: order; first match wins. Mirrors the §7 crew without importing it.
_ROLE_SIGNATURES: tuple[tuple[str, AgentRole], ...] = (
    ("adapter", AgentRole.ADAPTER),
    ("cinematograph", AgentRole.CINEMATOGRAPHER),
    ("cine", AgentRole.CINEMATOGRAPHER),
    ("continuity", AgentRole.CONTINUITY),
    ("critic", AgentRole.CRITIC),
    ("qa", AgentRole.CRITIC),
    ("showrunner", AgentRole.SHOWRUNNER),
    ("generator", AgentRole.GENERATOR),
    ("gen", AgentRole.GENERATOR),
)


def role_for_prompt_key(prompt_key: str) -> AgentRole:
    """Map a ``prompt_key`` to its crew :class:`AgentRole` (``UNKNOWN`` if none)."""
    key = prompt_key.lower()
    for sig, role in _ROLE_SIGNATURES:
        if sig in key:
            return role
    return AgentRole.UNKNOWN


# --------------------------------------------------------------------------- #
# Signal projection
# --------------------------------------------------------------------------- #


def _qa_from_dict(raw: dict | None) -> QAVerdict | None:
    if not raw:
        return None
    verdict = raw.get("verdict")
    passed = bool(raw.get("passed")) if "passed" in raw else (str(verdict).lower() == "pass")
    return QAVerdict(
        passed=passed,
        score=float(raw.get("score", 0.0) or 0.0),
        ccs=_opt_float(raw.get("ccs")),
        style_drift=_opt_float(raw.get("style_drift")),
        motion_artifact=_opt_float(raw.get("motion_artifact")),
        reward=_opt_float(raw.get("learned_reward", raw.get("reward"))),
        repair_action=raw.get("repair_action"),
        reason=str(raw.get("reason", "") or ""),
    )


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _edits_from_dicts(raws: Iterable[dict]) -> tuple[DirectorEdit, ...]:
    edits: list[DirectorEdit] = []
    for raw in raws:
        instruction = str(raw.get("instruction", raw.get("note", "")) or "").strip()
        if not instruction:
            continue
        edited_at = raw.get("edited_at")
        if isinstance(edited_at, str):
            try:
                edited_at = datetime.fromisoformat(edited_at)
            except ValueError:
                edited_at = None
        elif not isinstance(edited_at, datetime):
            edited_at = None
        edits.append(
            DirectorEdit(
                instruction=instruction,
                region=raw.get("region") or raw.get("region_png"),
                before=raw.get("before"),
                after=raw.get("after"),
                edited_at=edited_at,
            )
        )
    return tuple(edits)


def derive_reward(qa: QAVerdict | None, edits: tuple[DirectorEdit, ...]) -> float | None:
    """A default scalar reward from the QA verdict + director-edit pressure.

    The reward facet may override; this is the sensible default the pipeline
    attaches so a ``PREFERENCE`` example always carries *some* signal:

    * Start from the Critic's learned reward when present, else its overall score,
      else 1.0 for a pass / 0.0 for a fail.
    * Every director edit is evidence the output was wrong → a multiplicative
      penalty (a heavily-edited output earns near-zero reward even if QA passed).
    """
    if qa is None and not edits:
        return None
    base: float
    if qa is not None and qa.reward is not None:
        base = qa.reward
    elif qa is not None and qa.score:
        base = qa.score
    elif qa is not None:
        base = 1.0 if qa.passed else 0.0
    else:
        base = 1.0  # no QA but edits exist → treat the pre-edit output as a 1.0 baseline
    penalty = 0.6 ** len(edits)
    return round(max(0.0, min(1.0, base)) * penalty, 6)


# --------------------------------------------------------------------------- #
# Ingest configuration + normalizer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IngestConfig:
    """Knobs for the normalizer (all defaulted to the safe, lossless choices)."""

    #: Drop traces whose call errored (no usable output).
    drop_errors: bool = True
    #: Drop cache-hit traces (a replay, not a fresh behaviour).
    drop_cache_hits: bool = True
    #: Drop traces with an empty output.
    drop_empty_output: bool = True
    #: When set, force every example to this task; otherwise infer per-trace.
    force_task: TaskType | None = None
    #: Attach a default derived reward when a signal is present.
    derive_reward: bool = True


def infer_task(qa: QAVerdict | None, edits: tuple[DirectorEdit, ...]) -> TaskType:
    """Infer the learning task from the available supervision signal.

    A trace carrying a reward signal (QA verdict and/or a director edit) is
    ``PREFERENCE`` fuel; an unlabelled clean trace is ``SFT`` (imitation).
    """
    if qa is not None or edits:
        return TaskType.PREFERENCE
    return TaskType.SFT


def example_id(raw: RawTrace, role: AgentRole, task: TaskType) -> str:
    """A stable, idempotent id for the example a raw trace produces."""
    digest = stable_hash(
        {
            "trace_id": raw.trace_id,
            "prompt_key": raw.prompt_key,
            "prompt_version": raw.prompt_version,
            "role": role.value,
            "task": task.value,
        }
    )
    return f"ex_{digest[:24]}"


def normalize(raw: RawTrace, *, config: IngestConfig | None = None) -> TraceExample | None:
    """Normalize one :class:`RawTrace` → :class:`TraceExample` (or ``None`` if dropped).

    Returns ``None`` when the drop policy filters the trace; raises
    :class:`IngestError` only on a genuinely malformed record.
    """
    cfg = config or IngestConfig()
    try:
        if cfg.drop_errors and raw.error:
            return None
        if cfg.drop_cache_hits and raw.cache_hit:
            return None
        if cfg.drop_empty_output and not (raw.output or "").strip():
            return None

        role = role_for_prompt_key(raw.prompt_key)
        qa = _qa_from_dict(dict(raw.qa) if raw.qa else None)
        edits = _edits_from_dicts(dict(e) for e in raw.director_edits)
        task = cfg.force_task or infer_task(qa, edits)
        reward = derive_reward(qa, edits) if cfg.derive_reward else None

        return TraceExample(
            id=example_id(raw, role, task),
            role=role,
            task=task,
            prompt_key=raw.prompt_key,
            prompt_version=raw.prompt_version,
            model=raw.model,
            input=dict(raw.inputs),
            output=raw.output,
            qa=qa,
            director_edits=edits,
            reward=reward,
            split=Split.UNASSIGNED,
            trace_id=raw.trace_id,
            book_id=raw.book_id,
            session_id=raw.session_id,
            created_at=raw.created_at,
        )
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a typed ingest error
        raise IngestError(f"failed to normalize trace {raw.trace_id!r}: {exc}") from exc


@dataclass
class IngestStats:
    """A tally of an ingest run (for observability + the API report)."""

    seen: int = 0
    kept: int = 0
    dropped_error: int = 0
    dropped_cache: int = 0
    dropped_empty: int = 0
    by_role: dict[str, int] = field(default_factory=dict)
    by_task: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "seen": self.seen,
            "kept": self.kept,
            "dropped_error": self.dropped_error,
            "dropped_cache": self.dropped_cache,
            "dropped_empty": self.dropped_empty,
            "by_role": dict(self.by_role),
            "by_task": dict(self.by_task),
        }


def ingest_all(
    raws: Iterable[RawTrace], *, config: IngestConfig | None = None
) -> tuple[list[TraceExample], IngestStats]:
    """Normalize a stream of raw traces, returning the examples + a stats tally."""
    cfg = config or IngestConfig()
    stats = IngestStats()
    out: list[TraceExample] = []
    for raw in raws:
        stats.seen += 1
        if cfg.drop_errors and raw.error:
            stats.dropped_error += 1
            continue
        if cfg.drop_cache_hits and raw.cache_hit:
            stats.dropped_cache += 1
            continue
        if cfg.drop_empty_output and not (raw.output or "").strip():
            stats.dropped_empty += 1
            continue
        ex = normalize(raw, config=cfg)
        if ex is None:
            continue
        out.append(ex)
        stats.kept += 1
        stats.by_role[ex.role.value] = stats.by_role.get(ex.role.value, 0) + 1
        stats.by_task[ex.task.value] = stats.by_task.get(ex.task.value, 0) + 1
    return out, stats


__all__ = [
    "IngestConfig",
    "IngestStats",
    "derive_reward",
    "example_id",
    "infer_task",
    "ingest_all",
    "normalize",
    "role_for_prompt_key",
]
