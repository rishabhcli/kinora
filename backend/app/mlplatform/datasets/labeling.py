"""Labeling + weak supervision over training examples.

Most traces arrive without an explicit label; weak supervision turns cheap,
noisy heuristics into a single probabilistic label per example. A **labeling
function** (LF) looks at one example and votes a label or *abstains*; a **label
model** aggregates the votes of many LFs into a consensus label + a confidence,
accounting for each LF's estimated accuracy and coverage.

This is the deterministic, Snorkel-style core (no learning loop, no model
calls): LFs are pure functions, the label model is weighted majority vote with
accuracy weights estimated by agreement with the others (a closed-form proxy for
the generative model), and everything is reproducible.

Bundled LFs target the signals the alignment facet wants:

* ``lf_qa_pass`` / ``lf_qa_fail`` — vote ``good`` / ``bad`` from the Critic verdict.
* ``lf_director_edited`` — a director-edited output is ``bad`` (a human corrected it).
* ``lf_high_reward`` / ``lf_low_reward`` — vote from the derived reward bands.
* ``lf_empty_or_short`` — degenerate outputs are ``bad``.
* ``lf_valid_json`` — for the JSON-emitting agents, an unparseable output is ``bad``.

The output is examples carrying a ``label`` (the consensus) + ``label_conf`` in
their ``weak_labels`` map, plus a coverage/conflict report.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.mlplatform.datasets.contracts import AgentRole, TraceExample
from app.mlplatform.datasets.errors import LabelError

#: An LF returns a label string or ``None`` to abstain.
LabelVote = str | None
LabelingFunction = Callable[[TraceExample], LabelVote]

#: The binary quality label space the bundled LFs vote in.
GOOD = "good"
BAD = "bad"
ABSTAIN: LabelVote = None


@dataclass(frozen=True, slots=True)
class LF:
    """A named labeling function with its vote space."""

    name: str
    fn: LabelingFunction

    def __call__(self, ex: TraceExample) -> LabelVote:
        return self.fn(ex)


# --------------------------------------------------------------------------- #
# Bundled labeling functions
# --------------------------------------------------------------------------- #


def lf_qa_pass(ex: TraceExample) -> LabelVote:
    if ex.qa is None:
        return ABSTAIN
    return GOOD if ex.qa.passed else ABSTAIN


def lf_qa_fail(ex: TraceExample) -> LabelVote:
    if ex.qa is None:
        return ABSTAIN
    return BAD if not ex.qa.passed else ABSTAIN


def lf_director_edited(ex: TraceExample) -> LabelVote:
    return BAD if ex.director_edits else ABSTAIN


def lf_high_reward(ex: TraceExample) -> LabelVote:
    if ex.reward is None:
        return ABSTAIN
    return GOOD if ex.reward >= 0.8 else ABSTAIN


def lf_low_reward(ex: TraceExample) -> LabelVote:
    if ex.reward is None:
        return ABSTAIN
    return BAD if ex.reward <= 0.3 else ABSTAIN


def lf_empty_or_short(ex: TraceExample) -> LabelVote:
    return BAD if len((ex.output or "").strip()) < 8 else ABSTAIN


_JSON_ROLES = {AgentRole.ADAPTER, AgentRole.CINEMATOGRAPHER, AgentRole.CRITIC}


def lf_valid_json(ex: TraceExample) -> LabelVote:
    """For JSON-emitting agents: unparseable output is ``bad``; valid abstains."""
    if ex.role not in _JSON_ROLES:
        return ABSTAIN
    text = (ex.output or "").strip()
    if not text:
        return ABSTAIN
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return BAD
    return ABSTAIN


def default_lfs() -> tuple[LF, ...]:
    """The bundled labeling-function suite for the quality label."""
    return (
        LF("qa_pass", lf_qa_pass),
        LF("qa_fail", lf_qa_fail),
        LF("director_edited", lf_director_edited),
        LF("high_reward", lf_high_reward),
        LF("low_reward", lf_low_reward),
        LF("empty_or_short", lf_empty_or_short),
        LF("valid_json", lf_valid_json),
    )


# --------------------------------------------------------------------------- #
# The label model — weighted majority vote with estimated accuracies
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LFStat:
    """Coverage / overlap / conflict / estimated-accuracy of one LF."""

    name: str
    coverage: float
    overlap: float
    conflict: float
    est_accuracy: float
    votes: int

    def to_dict(self) -> dict[str, float | str | int]:
        return {
            "name": self.name,
            "coverage": round(self.coverage, 6),
            "overlap": round(self.overlap, 6),
            "conflict": round(self.conflict, 6),
            "est_accuracy": round(self.est_accuracy, 6),
            "votes": self.votes,
        }


@dataclass(frozen=True, slots=True)
class LabelReport:
    """The weak-supervision summary across a dataset."""

    n: int
    labeled: int
    abstained: int
    lf_stats: tuple[LFStat, ...]
    label_dist: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return self.labeled / self.n if self.n else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "labeled": self.labeled,
            "abstained": self.abstained,
            "coverage": round(self.coverage, 6),
            "label_dist": dict(self.label_dist),
            "lf_stats": [s.to_dict() for s in self.lf_stats],
        }


@dataclass
class LabelModel:
    """Weighted majority vote over a set of LFs (a closed-form Snorkel proxy).

    LF accuracy is estimated by each LF's *agreement rate with the majority of
    the others* on the examples it votes on — a learning-free proxy for the
    generative label model's accuracy parameter. The consensus label is the
    accuracy-weighted plurality; confidence is the winning weight share.
    """

    lfs: Sequence[LF] = field(default_factory=default_lfs)
    label_key: str = "quality"

    def __post_init__(self) -> None:
        if not self.lfs:
            raise LabelError("LabelModel needs at least one labeling function")
        names = [lf.name for lf in self.lfs]
        if len(names) != len(set(names)):
            raise LabelError("LF names must be unique")

    def _vote_matrix(self, examples: Sequence[TraceExample]) -> list[list[LabelVote]]:
        return [[lf(ex) for lf in self.lfs] for ex in examples]

    def _estimate_accuracies(self, votes: list[list[LabelVote]]) -> list[float]:
        """Each LF's agreement with the unweighted majority of the others."""
        m = len(self.lfs)
        agree = [0] * m
        total = [0] * m
        for row in votes:
            for i in range(m):
                if row[i] is None:
                    continue
                others = [row[j] for j in range(m) if j != i and row[j] is not None]
                if not others:
                    continue
                # Majority of the others (ties → no clear majority, skip).
                tally: dict[str, int] = {}
                for v in others:
                    assert v is not None
                    tally[v] = tally.get(v, 0) + 1
                top = max(tally.values())
                winners = [k for k, c in tally.items() if c == top]
                if len(winners) != 1:
                    continue
                total[i] += 1
                if row[i] == winners[0]:
                    agree[i] += 1
        # Laplace-smoothed accuracy; LFs that never co-vote default to 0.6.
        return [(agree[i] + 1) / (total[i] + 2) if total[i] else 0.6 for i in range(m)]

    def fit_predict(
        self, examples: Sequence[TraceExample]
    ) -> tuple[list[TraceExample], LabelReport]:
        """Estimate LF accuracies, then label every example by weighted vote."""
        if not examples:
            return [], LabelReport(n=0, labeled=0, abstained=0, lf_stats=())
        votes = self._vote_matrix(examples)
        acc = self._estimate_accuracies(votes)

        labeled: list[TraceExample] = []
        labeled_count = 0
        label_dist: dict[str, int] = {}
        for ex, row in zip(examples, votes, strict=True):
            weights: dict[str, float] = {}
            for i, v in enumerate(row):
                if v is None:
                    continue
                # log-odds weight: an accurate LF contributes more.
                weights[v] = weights.get(v, 0.0) + max(acc[i] - 0.5, 1e-3)
            if not weights:
                labeled.append(ex)
                continue
            total_w = sum(weights.values())
            best_label = max(weights.items(), key=lambda kv: (kv[1], kv[0]))
            conf = best_label[1] / total_w if total_w else 0.0
            labeled.append(
                ex.with_labels(
                    labels={self.label_key: best_label[0]},
                    weak_labels={
                        self.label_key: best_label[0],
                        f"{self.label_key}_conf": round(conf, 6),
                    },
                )
            )
            labeled_count += 1
            label_dist[best_label[0]] = label_dist.get(best_label[0], 0) + 1

        return labeled, self._report(examples, votes, acc, labeled_count, label_dist)

    def _report(
        self,
        examples: Sequence[TraceExample],
        votes: list[list[LabelVote]],
        acc: list[float],
        labeled_count: int,
        label_dist: dict[str, int],
    ) -> LabelReport:
        n = len(examples)
        stats: list[LFStat] = []
        for i, lf in enumerate(self.lfs):
            voted = sum(1 for row in votes if row[i] is not None)
            overlap = sum(
                1
                for row in votes
                if row[i] is not None
                and any(row[j] is not None for j in range(len(self.lfs)) if j != i)
            )
            conflict = sum(
                1
                for row in votes
                if row[i] is not None
                and any(
                    row[j] is not None and row[j] != row[i]
                    for j in range(len(self.lfs))
                    if j != i
                )
            )
            stats.append(
                LFStat(
                    name=lf.name,
                    coverage=voted / n if n else 0.0,
                    overlap=overlap / n if n else 0.0,
                    conflict=conflict / n if n else 0.0,
                    est_accuracy=acc[i],
                    votes=voted,
                )
            )
        return LabelReport(
            n=n,
            labeled=labeled_count,
            abstained=n - labeled_count,
            lf_stats=tuple(stats),
            label_dist=label_dist,
        )


def apply_labeling(
    examples: Sequence[TraceExample], *, model: LabelModel | None = None
) -> tuple[list[TraceExample], LabelReport]:
    """Convenience: fit the (default) label model and label the examples."""
    return (model or LabelModel()).fit_predict(examples)


__all__ = [
    "ABSTAIN",
    "BAD",
    "GOOD",
    "LF",
    "LFStat",
    "LabelModel",
    "LabelReport",
    "LabelVote",
    "LabelingFunction",
    "apply_labeling",
    "default_lfs",
    "lf_director_edited",
    "lf_empty_or_short",
    "lf_high_reward",
    "lf_low_reward",
    "lf_qa_fail",
    "lf_qa_pass",
    "lf_valid_json",
]
