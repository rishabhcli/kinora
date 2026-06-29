"""Plan-regression guard — the CI gate that keeps the hot paths on good plans.

A *baseline* is a captured snapshot of a query's EXPLAIN plan: its total cost, the
multiset of node types, the relations scanned, and whether it used a sequential
scan. The guard compares a *fresh* plan against the stored baseline and flags a
regression when any of:

* the total cost grew beyond a tolerance factor (default 1.5×),
* a **new sequential scan** appeared (a previously-indexed path lost its index —
  the §4.2 source-span seek must stay a btree ``Index Scan``),
* the node-type shape changed in a way that usually signals a worse plan (a
  ``Nested Loop`` became a ``Seq Scan``-fed ``Hash Join`` over a big table, an
  ``Index Scan`` degraded to a ``Seq Scan``, a ``Sort`` appeared, …).

Baselines are plain JSON, so they live in the repo and are reviewed like any other
fixture; :class:`BaselineStore` loads/saves a directory of them. The guard itself
is pure: it takes two :class:`PlanSnapshot`\\s (or a baseline + a live
:class:`~app.db.inspect.QueryPlan`) and returns a structured :class:`PlanDiff`.

This module imports nothing heavy and runs anywhere; the live capture path
(turning a ``QueryPlan`` into a snapshot) is a thin adapter over the existing
EXPLAIN inspector.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.datascale.optimize.errors import RegressionDetected
from app.datascale.optimize.fingerprint import make_fingerprint

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.inspect import QueryPlan

#: Default cost-growth factor beyond which a plan is flagged.
DEFAULT_COST_TOLERANCE = 1.5

#: Node-type transitions that are almost always a regression when newly present.
_REGRESSION_NODES = frozenset({"Seq Scan"})


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """A captured, comparable summary of one query's EXPLAIN plan."""

    fingerprint: str
    skeleton: str
    total_cost: float
    node_types: tuple[str, ...]
    relations: tuple[str, ...]
    used_seq_scan: bool

    def node_counter(self) -> Counter[str]:
        """Multiset of node types (order-independent comparison)."""
        return Counter(self.node_types)

    def to_json(self) -> dict[str, Any]:
        """JSON-serialisable form for on-disk baselines."""
        return {
            "fingerprint": self.fingerprint,
            "skeleton": self.skeleton,
            "total_cost": self.total_cost,
            "node_types": list(self.node_types),
            "relations": list(self.relations),
            "used_seq_scan": self.used_seq_scan,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PlanSnapshot:
        """Reconstruct a snapshot from its JSON form."""
        return cls(
            fingerprint=str(data["fingerprint"]),
            skeleton=str(data["skeleton"]),
            total_cost=float(data["total_cost"]),
            node_types=tuple(data.get("node_types", [])),
            relations=tuple(data.get("relations", [])),
            used_seq_scan=bool(data.get("used_seq_scan", False)),
        )


def snapshot_from_plan(sql: str, plan: QueryPlan) -> PlanSnapshot:
    """Capture a :class:`PlanSnapshot` from a live EXPLAIN :class:`QueryPlan`."""
    qf = make_fingerprint(sql)
    nodes = plan.root.walk()
    relations = tuple(sorted({n.relation for n in nodes if n.relation}))
    return PlanSnapshot(
        fingerprint=qf.hexdigest,
        skeleton=qf.skeleton,
        total_cost=plan.total_cost,
        node_types=tuple(n.node_type for n in nodes),
        relations=relations,
        used_seq_scan=plan.used_seq_scan,
    )


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PlanDiff:
    """The structured difference between a baseline and a fresh plan."""

    fingerprint: str
    baseline_cost: float
    current_cost: float
    cost_ratio: float
    new_seq_scan: bool
    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        """True when any regression reason was recorded."""
        return bool(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint[:12],
            "baseline_cost": round(self.baseline_cost, 2),
            "current_cost": round(self.current_cost, 2),
            "cost_ratio": round(self.cost_ratio, 3),
            "new_seq_scan": self.new_seq_scan,
            "added_nodes": self.added_nodes,
            "removed_nodes": self.removed_nodes,
            "regressed": self.regressed,
            "reasons": self.reasons,
        }


def compare_plans(
    baseline: PlanSnapshot,
    current: PlanSnapshot,
    *,
    cost_tolerance: float = DEFAULT_COST_TOLERANCE,
) -> PlanDiff:
    """Compare a fresh plan against a baseline and report a :class:`PlanDiff`."""
    base_cost = max(baseline.total_cost, 1e-9)
    ratio = current.total_cost / base_cost
    base_counter = baseline.node_counter()
    cur_counter = current.node_counter()

    added = sorted((cur_counter - base_counter).elements())
    removed = sorted((base_counter - cur_counter).elements())
    new_seq_scan = current.used_seq_scan and not baseline.used_seq_scan

    reasons: list[str] = []
    if ratio > cost_tolerance:
        reasons.append(
            f"cost grew {ratio:.2f}× ({baseline.total_cost:.0f} → {current.total_cost:.0f}), "
            f"tolerance {cost_tolerance:.2f}×"
        )
    if new_seq_scan:
        reasons.append("a new sequential scan appeared (lost an index?)")
    regression_nodes = [n for n in added if n in _REGRESSION_NODES and not new_seq_scan]
    for node in regression_nodes:
        reasons.append(f"new {node!r} node in the plan")

    return PlanDiff(
        fingerprint=current.fingerprint,
        baseline_cost=baseline.total_cost,
        current_cost=current.total_cost,
        cost_ratio=ratio,
        new_seq_scan=new_seq_scan,
        added_nodes=added,
        removed_nodes=removed,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Baseline store
# --------------------------------------------------------------------------- #


class BaselineStore:
    """Loads/saves a directory of plan baselines as one JSON file per fingerprint.

    In-memory by default; :meth:`load` / :meth:`save` persist to a directory so a
    repo can check baselines in. Keyed by fingerprint, so re-capturing the same
    query shape updates its baseline in place.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory is not None else None
        self._baselines: dict[str, PlanSnapshot] = {}

    def put(self, snapshot: PlanSnapshot) -> None:
        """Store (or replace) a baseline in memory."""
        self._baselines[snapshot.fingerprint] = snapshot

    def get(self, fingerprint: str) -> PlanSnapshot | None:
        """Return a baseline by fingerprint (``None`` if absent)."""
        return self._baselines.get(fingerprint)

    def get_for_sql(self, sql: str) -> PlanSnapshot | None:
        """Return a baseline for a query's shape."""
        return self.get(make_fingerprint(sql).hexdigest)

    def __len__(self) -> int:
        return len(self._baselines)

    def __contains__(self, fingerprint: object) -> bool:
        return fingerprint in self._baselines

    def all(self) -> list[PlanSnapshot]:
        """All stored baselines (fingerprint-sorted)."""
        return [self._baselines[k] for k in sorted(self._baselines)]

    def load(self, directory: str | Path | None = None) -> int:
        """Load baselines from a directory of ``*.json`` files. Returns the count."""
        target = Path(directory) if directory is not None else self._dir
        if target is None:
            raise ValueError("no directory configured for the baseline store")
        if not target.exists():
            return 0
        count = 0
        for path in sorted(target.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            snap = PlanSnapshot.from_json(data)
            self._baselines[snap.fingerprint] = snap
            count += 1
        return count

    def save(self, directory: str | Path | None = None) -> int:
        """Write each baseline to ``<fingerprint>.json`` in the directory."""
        target = Path(directory) if directory is not None else self._dir
        if target is None:
            raise ValueError("no directory configured for the baseline store")
        target.mkdir(parents=True, exist_ok=True)
        for snap in self._baselines.values():
            path = target / f"{snap.fingerprint}.json"
            path.write_text(
                json.dumps(snap.to_json(), indent=2, sort_keys=True), encoding="utf-8"
            )
        return len(self._baselines)


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GuardReport:
    """The outcome of guarding a set of fresh plans against baselines."""

    diffs: list[PlanDiff]
    missing_baselines: list[str]

    @property
    def regressions(self) -> list[PlanDiff]:
        """Only the diffs that regressed."""
        return [d for d in self.diffs if d.regressed]

    @property
    def ok(self) -> bool:
        """True when nothing regressed."""
        return not self.regressions

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "regressions": [d.as_dict() for d in self.regressions],
            "checked": len(self.diffs),
            "missing_baselines": self.missing_baselines,
        }


class PlanRegressionGuard:
    """Compares fresh plan snapshots against a :class:`BaselineStore`."""

    def __init__(
        self, store: BaselineStore, *, cost_tolerance: float = DEFAULT_COST_TOLERANCE
    ) -> None:
        self._store = store
        self._tolerance = cost_tolerance

    def check(self, current: PlanSnapshot) -> PlanDiff | None:
        """Compare one fresh snapshot; ``None`` when no baseline exists for it."""
        baseline = self._store.get(current.fingerprint)
        if baseline is None:
            return None
        return compare_plans(baseline, current, cost_tolerance=self._tolerance)

    def check_all(self, snapshots: list[PlanSnapshot]) -> GuardReport:
        """Compare many snapshots, collecting diffs + any missing baselines."""
        diffs: list[PlanDiff] = []
        missing: list[str] = []
        for snap in snapshots:
            diff = self.check(snap)
            if diff is None:
                missing.append(snap.fingerprint)
            else:
                diffs.append(diff)
        return GuardReport(diffs=diffs, missing_baselines=missing)

    def assert_no_regression(self, current: PlanSnapshot) -> None:
        """Raise :class:`RegressionDetected` if ``current`` regressed (CI gate)."""
        diff = self.check(current)
        if diff is not None and diff.regressed:
            raise RegressionDetected(
                f"plan regression for {current.skeleton!r}: {'; '.join(diff.reasons)}",
                diff=diff,
            )


__all__ = [
    "DEFAULT_COST_TOLERANCE",
    "BaselineStore",
    "GuardReport",
    "PlanDiff",
    "PlanRegressionGuard",
    "PlanSnapshot",
    "compare_plans",
    "snapshot_from_plan",
]
