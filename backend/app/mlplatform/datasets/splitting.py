"""Leak-free, stratified train / val / test splitting.

A split is only honest if (a) the class balance is preserved across the three
partitions (*stratified*) and (b) no group of related examples straddles a
partition boundary (*leak-free*). Both matter here: traces from one book share a
canon, characters, and style, so a shot from book X in *train* and another shot
from the same book X in *test* leaks identity the model can memorise rather than
generalise — exactly the failure §13 warns the eval against.

The splitter therefore works at **group granularity** (default group =
``TraceExample.group_key``, which defaults to ``book_id``): every example in a
group lands in the same partition, and groups are dealt into partitions to hit
the target ratios while keeping each *stratum* (a label, e.g. the agent role or
QA verdict) balanced. Assignment is **deterministic** — a stable hash of the
group key seeded by a caller-supplied seed decides order — so the same dataset +
seed always produces the same split (reproducibility the eval protocol demands).

The output is a new :class:`Dataset` whose examples carry their assigned
:class:`Split`, plus a :class:`SplitReport` proving the ratios and the
no-leak invariant.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.mlplatform.datasets.contracts import Dataset, Split, TraceExample
from app.mlplatform.datasets.errors import SplitError

#: A stratum key groups examples that must stay balanced across splits.
StratumKey = Callable[[TraceExample], str]


def role_stratum(ex: TraceExample) -> str:
    return ex.role.value


def qa_stratum(ex: TraceExample) -> str:
    if ex.qa is None:
        return "no_qa"
    return "pass" if ex.qa.passed else "fail"


def role_task_stratum(ex: TraceExample) -> str:
    return f"{ex.role.value}/{ex.task.value}"


@dataclass(frozen=True, slots=True)
class SplitRatios:
    """Target fractions for the three partitions (must sum to ~1)."""

    train: float = 0.8
    val: float = 0.1
    test: float = 0.1

    def __post_init__(self) -> None:
        total = self.train + self.val + self.test
        if abs(total - 1.0) > 1e-6:
            raise SplitError(f"split ratios must sum to 1.0, got {total}")
        if min(self.train, self.val, self.test) < 0:
            raise SplitError("split ratios must be non-negative")

    def as_tuple(self) -> tuple[tuple[Split, float], ...]:
        return ((Split.TRAIN, self.train), (Split.VAL, self.val), (Split.TEST, self.test))


@dataclass(frozen=True, slots=True)
class SplitConfig:
    """How to split: ratios, the grouping key, the stratum key, the seed."""

    ratios: SplitRatios = field(default_factory=SplitRatios)
    #: Examples sharing this key stay together (default: ``group_key`` / book).
    group_of: Callable[[TraceExample], str] = lambda e: e.group_key
    #: Examples are balanced across splits within each stratum.
    stratum_of: StratumKey = role_stratum
    seed: int = 1729


def _rank(key: str, seed: int) -> float:
    """A stable [0,1) rank for a group key under a seed (deterministic order)."""
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True, slots=True)
class SplitReport:
    """Proof the split hit its ratios and leaked no group."""

    counts: Mapping[str, int]
    group_counts: Mapping[str, int]
    stratum_balance: Mapping[str, Mapping[str, int]]
    leaked_groups: tuple[str, ...] = ()
    total: int = 0

    @property
    def leak_free(self) -> bool:
        return not self.leaked_groups

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "counts": dict(self.counts),
            "group_counts": dict(self.group_counts),
            "stratum_balance": {k: dict(v) for k, v in self.stratum_balance.items()},
            "leaked_groups": list(self.leaked_groups),
            "leak_free": self.leak_free,
        }


def _assign_groups(
    group_to_stratum: dict[str, str], cfg: SplitConfig
) -> dict[str, Split]:
    """Deal groups into splits per stratum, deterministically, hitting ratios.

    Within each stratum, groups are ordered by their stable rank and walked while
    accumulating a target running fraction; this both balances strata and is
    fully reproducible. A stratum with a single group puts it in TRAIN (never
    starve the trainable set for a rare class).
    """
    by_stratum: dict[str, list[str]] = {}
    for group, stratum in group_to_stratum.items():
        by_stratum.setdefault(stratum, []).append(group)

    assignment: dict[str, Split] = {}
    targets = cfg.ratios.as_tuple()
    for groups in by_stratum.values():
        ordered = sorted(groups, key=lambda g: _rank(g, cfg.seed))
        n = len(ordered)
        if n == 1:
            assignment[ordered[0]] = Split.TRAIN
            continue
        # Cumulative boundaries: e.g. train up to 0.8, val up to 0.9, test rest.
        boundaries: list[tuple[Split, float]] = []
        acc = 0.0
        for split, frac in targets:
            acc += frac
            boundaries.append((split, acc))
        for i, group in enumerate(ordered):
            pos = (i + 0.5) / n
            chosen = boundaries[-1][0]
            for split, bound in boundaries:
                if pos <= bound:
                    chosen = split
                    break
            assignment[group] = chosen
    return assignment


def split_dataset(
    dataset: Dataset, *, config: SplitConfig | None = None
) -> tuple[Dataset, SplitReport]:
    """Assign every example a leak-free, stratified split; return the new dataset + report.

    Each *group* (book by default) is assigned to exactly one split, so no
    related example straddles the boundary. The report's ``leaked_groups`` is the
    invariant check — it is always empty for a correctly built split (the test
    asserts it), but it is recomputed from the *result* so a bug can't hide.
    """
    cfg = config or SplitConfig()
    if not dataset.examples:
        raise SplitError("cannot split an empty dataset")

    # A group's stratum is its majority stratum (groups are atomic; ties → first).
    group_strata: dict[str, dict[str, int]] = {}
    for ex in dataset.examples:
        g = cfg.group_of(ex)
        s = cfg.stratum_of(ex)
        group_strata.setdefault(g, {})[s] = group_strata.setdefault(g, {}).get(s, 0) + 1
    group_to_stratum = {
        g: max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        for g, counts in group_strata.items()
    }

    assignment = _assign_groups(group_to_stratum, cfg)

    assigned = [ex.with_split(assignment[cfg.group_of(ex)]) for ex in dataset.examples]
    new_ds = Dataset(
        name=dataset.name,
        examples=tuple(assigned),
        description=dataset.description,
        meta={**dict(dataset.meta), "split_seed": cfg.seed},
    )

    # Build the report + verify no leak by recomputing group→split from the result.
    counts: dict[str, int] = {s.value: 0 for s in (Split.TRAIN, Split.VAL, Split.TEST)}
    group_counts: dict[str, int] = {s.value: 0 for s in (Split.TRAIN, Split.VAL, Split.TEST)}
    stratum_balance: dict[str, dict[str, int]] = {}
    group_splits: dict[str, set[str]] = {}
    for ex in assigned:
        counts[ex.split.value] += 1
        g = cfg.group_of(ex)
        group_splits.setdefault(g, set()).add(ex.split.value)
        s = cfg.stratum_of(ex)
        stratum_balance.setdefault(s, {s2.value: 0 for s2 in (Split.TRAIN, Split.VAL, Split.TEST)})
        stratum_balance[s][ex.split.value] += 1
    for split in assignment.values():
        group_counts[split.value] += 1
    leaked = tuple(sorted(g for g, splits in group_splits.items() if len(splits) > 1))

    report = SplitReport(
        counts=counts,
        group_counts=group_counts,
        stratum_balance=stratum_balance,
        leaked_groups=leaked,
        total=len(assigned),
    )
    if leaked:  # pragma: no cover - invariant guard; should be unreachable
        raise SplitError(f"split leaked groups across partitions: {leaked[:5]}")
    return new_ds, report


__all__ = [
    "SplitConfig",
    "SplitRatios",
    "SplitReport",
    "StratumKey",
    "qa_stratum",
    "role_stratum",
    "role_task_stratum",
    "split_dataset",
]
