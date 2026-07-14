#!/usr/bin/env python3
"""Aggregate all 10 books' export-review manifests into one campaign REPORT.md.

Each book's live-run grading pass (``kinora-admin books export-review``, see
``app/cli/actions/review_export.py``) writes a ``manifest.json`` into its own
subdirectory of the campaign root. This script walks every subdirectory,
pulls the per-book shot/QA/long-range-findings numbers out of each manifest,
and writes a single cross-book Markdown table next to them:

    backend/.venv/bin/python backend/scripts/qa_campaign_report.py \\
        artifacts/qa/10-book-campaign
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/qa/10-book-campaign")


def main() -> int:
    rows = []
    for manifest_path in sorted(ROOT.glob("*/manifest.json")):
        m = json.loads(manifest_path.read_text())
        shots = m["shots"]
        accepted = sum(1 for s in shots if s["status"] == "accepted")
        ccs_values = [s["qa_ccs"] for s in shots if s.get("qa_ccs") is not None]
        rows.append(
            {
                "title": m["title"],
                "shots": len(shots),
                "accepted": accepted,
                "accept_rate": round(accepted / len(shots), 3) if shots else 0.0,
                "mean_ccs": round(sum(ccs_values) / len(ccs_values), 3) if ccs_values else None,
                "long_range_findings": len(m.get("long_range_findings", [])),
            }
        )
    lines = ["# 10-Book QA Campaign Report", ""]
    lines.append("| Book | Shots | Accepted | Accept Rate | Mean CCS | Long-range findings |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['title']} | {r['shots']} | {r['accepted']} | {r['accept_rate']} | "
            f"{r['mean_ccs']} | {r['long_range_findings']} |"
        )
    (ROOT / "REPORT.md").write_text("\n".join(lines))
    print(f"wrote {ROOT / 'REPORT.md'} covering {len(rows)} books")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
