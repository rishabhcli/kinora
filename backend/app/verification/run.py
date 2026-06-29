"""Run every protocol spec and print a consolidated report.

``python -m app.verification.run`` checks all three specs (scheduler buffer,
render queue, arbitration), printing each property's verdict and, for any
failure, the counterexample trace / lasso. It exits non-zero if any property
fails, so it doubles as a CI gate. The textual output is what gets pasted into
``DESIGN.md`` under "checked specs + results".

This module is pure orchestration: it builds the specs, runs the checker, and
formats. It never touches infra and spends no credits.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from app.verification.modelcheck import CheckReport, ModelChecker
from app.verification.modelcheck.spec import Spec
from app.verification.modelcheck.symmetry import SymmetryReduction
from app.verification.specs import (
    build_arbitration_spec,
    build_fairness_spec,
    build_render_queue_spec,
    build_scheduler_buffer_spec,
    session_symmetry,
)

__all__ = ["all_specs", "check_all", "main"]


def all_specs() -> list[tuple[Spec[Any], SymmetryReduction | None]]:
    """Every protocol spec, in report order, paired with its symmetry reduction.

    A ``None`` reduction explores the full space; the fairness spec carries the
    interchangeable-sessions reduction so its multi-session space stays small.
    """
    return [
        (build_scheduler_buffer_spec(), None),
        (build_render_queue_spec(workers=2), None),
        (build_arbitration_spec(), None),
        (build_fairness_spec(sessions=3), session_symmetry()),
    ]


def check_all(
    specs: Sequence[tuple[Spec[Any], SymmetryReduction | None]] | None = None,
) -> list[CheckReport[Any]]:
    """Check every spec (under its symmetry reduction) and return the reports."""
    specs = list(specs) if specs is not None else all_specs()
    reports: list[CheckReport[Any]] = []
    for spec, reduction in specs:
        checker = ModelChecker[Any](symmetry=reduction)
        reports.append(checker.check(spec))
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: check all specs, print the consolidated report, return status."""
    _ = argv
    reports = check_all()
    blocks: list[str] = []
    all_ok = True
    for report in reports:
        blocks.append(report.render())
        all_ok = all_ok and report.ok
    print("\n\n".join(blocks))
    print()
    if all_ok:
        print("ALL SPECS HOLD ✓")
        return 0
    failed = [r.spec_name for r in reports if not r.ok]
    print(f"PROPERTY VIOLATIONS in: {', '.join(failed)}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
