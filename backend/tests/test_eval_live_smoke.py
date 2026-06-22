"""Optional LIVE crew-vs-baseline smoke (guarded; never required for the suite).

Runs the real §13 protocol on a couple of shots of a real, already-ingested book:
the crew arm executes the real per-shot render pipeline on the degradation path
(``KINORA_LIVE_VIDEO`` off → **zero Wan video-seconds**, real keyframes), and the
baseline arm calls the real image model for its frame-chained stills. It exercises
DashScope + Postgres + object storage end-to-end, so it is skipped unless both
``KINORA_LIVE_TESTS=1`` and ``KINORA_EVAL_BOOK_ID=<book>`` are set.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KINORA_LIVE_TESTS"),
    reason="live crew-vs-baseline smoke; set KINORA_LIVE_TESTS=1 (and KINORA_EVAL_BOOK_ID)",
)


async def test_live_crew_vs_baseline_smoke() -> None:
    book_id = os.environ.get("KINORA_EVAL_BOOK_ID")
    if not book_id:
        pytest.skip("set KINORA_EVAL_BOOK_ID to a real, ingested book id")

    from app.eval.run import run_eval

    report = await run_eval(book_id, runs=1, max_shots=2, write_cache=False)
    contract = report.to_contract()

    assert set(contract["ccs"].keys()) == {"crew", "baseline"}
    assert contract["runs"] == 1
    assert contract["thresholds"]["ccs_min"] == 0.85
    # Memory + locked references should make the crew at least as consistent as the
    # single-agent baseline, with zero video-seconds spent on either arm.
    assert contract["ccs"]["crew"] >= contract["ccs"]["baseline"]
