"""Unit tests for the offline ``python -m app.llmops.run`` CLI (no infra)."""

from __future__ import annotations

import json

from app.llmops.run import build_parser, main, run_report


async def test_eval_mode_report() -> None:
    args = build_parser().parse_args(
        ["--key", "adapter", "--dataset", "adapter_golden_v1", "--runs", "2"]
    )
    payload = await run_report(args)
    assert payload["mode"] == "eval"
    assert payload["report"]["prompt_key"] == "adapter"
    assert payload["report"]["runs"] == 2


async def test_ab_mode_report() -> None:
    # A/B a seeded version against itself (the CLI builds its own fresh service,
    # so only seeded baseline versions are available): a self-comparison is a tie.
    args = build_parser().parse_args(
        ["--ab", "1.0.0", "--version", "1.0.0", "--dataset", "adapter_golden_v1", "--runs", "1"]
    )
    payload = await run_report(args)
    assert payload["mode"] == "ab"
    assert payload["report"]["winner"] == "tie"


def test_main_writes_out(tmp_path: object) -> None:
    out = f"{tmp_path}/report.json"
    code = main(["--key", "adapter", "--dataset", "adapter_golden_v1", "--runs", "1", "--out", out])
    assert code == 0
    with open(out, encoding="utf-8") as fh:
        body = json.load(fh)
    assert body["mode"] == "eval"


def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.key == "adapter"
    assert args.dataset == "adapter_golden_v1"
    assert args.runs == 3
