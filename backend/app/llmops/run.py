"""``python -m app.llmops.run`` — offline LLM-ops eval CLI.

The operator entrypoint for evaluating a prompt version against a golden dataset
*without a live model*. It seeds the registry from the crew's prompts, runs the
deterministic fake responder + judge over a dataset, and prints the §13-style
report (mean ± spread over ``--runs``) as JSON. Two modes:

* ``--eval`` (default) — evaluate one version against one dataset.
* ``--ab VERSION_B`` — A/B the active (or ``--version``) version against
  ``VERSION_B``.
* ``--regression VERSION`` — register-then-check a candidate against the baseline.

Because everything is offline (no Postgres, no Redis, no provider), this *is*
exercised by the unit suite — unlike ``app.eval.run``, which is infra-backed.
Subcommands compose the same :class:`~app.llmops.service.LLMOpsService` the API
uses, so the CLI and the API agree by construction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.llmops.service import LLMOpsService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.llmops.run", description=__doc__)
    parser.add_argument("--key", default="adapter", help="prompt key to evaluate")
    parser.add_argument(
        "--dataset", default="adapter_golden_v1", help="golden dataset name to score against"
    )
    parser.add_argument("--version", default=None, help="prompt version (default: active)")
    parser.add_argument("--runs", type=int, default=3, help="number of runs (mean + spread)")
    parser.add_argument("--ab", default=None, metavar="VERSION_B", help="A/B vs this version")
    parser.add_argument(
        "--regression", default=None, metavar="VERSION", help="regression-check this candidate"
    )
    parser.add_argument("--out", default=None, help="write the JSON report to this path")
    return parser


async def run_report(args: argparse.Namespace) -> dict[str, Any]:
    """Build the requested report dict using the offline service."""
    svc = LLMOpsService.create()
    if args.ab is not None:
        version_a = args.version or svc.active_prompt(args.key).version
        result = await svc.ab_test(
            prompt_key=args.key,
            version_a=version_a,
            version_b=args.ab,
            dataset_name=args.dataset,
            runs=args.runs,
        )
        return {"mode": "ab", "report": result.to_dict()}
    if args.regression is not None:
        verdict, baseline, candidate = await svc.check_regression(
            prompt_key=args.key,
            candidate_version=args.regression,
            dataset_name=args.dataset,
            baseline_version=args.version,
            runs=args.runs,
        )
        return {
            "mode": "regression",
            "verdict": verdict.to_dict(),
            "baseline": baseline.to_dict(),
            "candidate": candidate.to_dict(),
        }
    report = await svc.evaluate(
        prompt_key=args.key,
        dataset_name=args.dataset,
        version=args.version,
        runs=args.runs,
    )
    return {"mode": "eval", "report": report.to_dict()}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = asyncio.run(run_report(args))
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
    print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
