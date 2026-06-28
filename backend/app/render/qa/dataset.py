"""Reward-dataset seam — turn accumulated QA outcomes into training rows (§8.2, §8.6).

The learned-reward layer (``app/render/reward.py``) trains on the accept/reject
signal the system already accumulates in episodic memory: every accepted shot vs.
every degraded shot (the retry cap fell through ⇒ the director got a degraded clip
⇒ an *implicit reject*), plus director edits (a re-coloured/re-framed shot is an
implicit reject-with-correction of the original).

This module defines the **seam** the calibration pass reads over — a small
:class:`RewardSignalSource` Protocol satisfied by the existing
:class:`~app.memory.episodic_service.EpisodicService` *without changing it* — and a
pure adapter, :func:`build_reward_dataset`, that maps the stored ``qa`` dicts + a
``status``/label into :class:`~app.render.reward.QASample` rows. It reaches into NO
other domain's internals: it only consumes the public ``qa`` payload shape the Critic
already writes (``ccs`` / ``style_drift`` / ``timeline_ok`` / ``motion_artifact`` and
the optional ``aesthetic`` / ``temporal`` axes this subsystem added).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from app.render.reward import QASample

#: Episodic ``status`` values that count as the director keeping the clip.
ACCEPT_STATUSES = frozenset({"accepted"})
#: Episodic ``status`` values that count as an implicit reject (degraded / superseded).
REJECT_STATUSES = frozenset({"degraded", "rejected", "superseded"})


class QAOutcome(Protocol):
    """The minimal shape of one stored shot outcome the dataset reader needs.

    The episodic :class:`~app.db.models.shot.Shot` row satisfies this structurally —
    it exposes ``status`` and a ``qa`` JSON payload — so the reader never imports the
    ORM model or the repository; it depends only on this attribute surface.
    """

    @property
    def status(self) -> Any: ...

    @property
    def qa(self) -> dict[str, Any] | None: ...


class RewardSignalSource(Protocol):
    """The seam the calibration pass reads accumulated QA outcomes over.

    A thin wrapper over :class:`~app.memory.episodic_service.EpisodicService` (or a
    repository query) implements this; tests inject an in-memory list. It is read-only
    and returns already-persisted outcomes — no model call, no write-back.
    """

    async def recent_outcomes(self, book_id: str, *, limit: int = 500) -> Sequence[QAOutcome]:
        """Return up to ``limit`` recent shot outcomes for ``book_id`` (newest first)."""
        ...


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sample_from_qa(qa: dict[str, Any] | None, *, accepted: bool) -> QASample | None:
    """Map one stored ``qa`` payload + a label to a :class:`QASample` (or ``None``).

    Returns ``None`` when the payload lacks the core numeric fields (an incomplete or
    pre-QA row) so the dataset only ever contains real, fully-scored examples.
    """
    if not qa:
        return None
    if "ccs" not in qa or "style_drift" not in qa or "motion_artifact" not in qa:
        return None
    return QASample(
        ccs=_coerce_float(qa.get("ccs"), 1.0),
        style_drift=_coerce_float(qa.get("style_drift"), 0.0),
        timeline_ok=bool(qa.get("timeline_ok", True)),
        motion_artifact=_coerce_float(qa.get("motion_artifact"), 0.0),
        aesthetic=_coerce_float(qa.get("aesthetic"), 1.0),
        temporal=_coerce_float(qa.get("temporal"), 1.0),
        accepted=accepted,
    )


def label_for_status(status: Any) -> bool | None:
    """Map an episodic ``status`` to an accept(True)/reject(False) label, or ``None``.

    ``None`` means "not a terminal outcome" (e.g. still ``rendering``/``qa``) and the
    row is skipped — only clips the director actually saw kept or degraded teach.
    """
    name = getattr(status, "value", status)
    name = str(name).lower()
    if name in ACCEPT_STATUSES:
        return True
    if name in REJECT_STATUSES:
        return False
    return None


def build_reward_dataset(outcomes: Iterable[QAOutcome]) -> list[QASample]:
    """Map stored shot outcomes → labeled :class:`QASample` rows (pure adapter).

    Skips rows with no terminal accept/reject label and rows whose ``qa`` payload is
    incomplete, so the returned dataset is exactly the supervised signal the learned
    reward + threshold calibration train on.
    """
    samples: list[QASample] = []
    for outcome in outcomes:
        label = label_for_status(outcome.status)
        if label is None:
            continue
        sample = sample_from_qa(outcome.qa, accepted=label)
        if sample is not None:
            samples.append(sample)
    return samples


__all__ = [
    "ACCEPT_STATUSES",
    "REJECT_STATUSES",
    "QAOutcome",
    "RewardSignalSource",
    "build_reward_dataset",
    "label_for_status",
    "sample_from_qa",
]
