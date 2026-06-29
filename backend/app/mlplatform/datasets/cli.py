"""A self-contained, offline CLI for the dataset pipeline.

``python -m app.mlplatform.datasets.cli <command> [flags]``

Deliberately scoped to **this package** (it does not register into the global
``app.cli`` command surface, to avoid editing that shared subsystem). It drives
the pure pipeline against an in-memory :class:`DatasetService`, so it is fully
offline — no DB, no network, no credits — and is exercised end-to-end in the
tests. Its purpose is operability + a demonstrable build artifact: replay a
JSONL trace dump into a versioned, split, exported dataset and print the stats /
lineage / drift / diff as JSON.

Commands:

* ``build``   — read a JSONL trace dump → build a dataset → print the build report.
* ``stats``   — re-print a built dataset's stats (within one process; in-memory).
* ``export``  — build then emit JSONL/CSV for a split + shape.
* ``inspect`` — build then print the lineage walk + the final stats.

A trace dump is JSONL where each line is a ``RawTrace``-shaped object (the
``record`` export shape is *not* the input here; this consumes raw traces). For
hermetic demos, ``--demo N`` synthesises a corpus instead of reading a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from app.mlplatform.datasets.contracts import RawTrace, Split
from app.mlplatform.datasets.export import ExportShape
from app.mlplatform.datasets.pipeline import BuildConfig
from app.mlplatform.datasets.service import DatasetService
from app.mlplatform.datasets.sources import InMemoryTraceSource
from app.mlplatform.datasets.splitting import SplitConfig, SplitRatios


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


def raw_from_row(row: dict[str, Any], *, ordinal: int) -> RawTrace:
    """Build a :class:`RawTrace` from a permissive JSON row (the dump shape)."""
    return RawTrace(
        trace_id=str(row.get("trace_id", row.get("id", f"t{ordinal}"))),
        prompt_key=str(row.get("prompt_key", "unknown@v0")),
        prompt_version=str(row.get("prompt_version", "0.0.0")),
        model=str(row.get("model", "unknown")),
        inputs=dict(row.get("inputs", row.get("input", {}))),
        output=str(row.get("output", "")),
        created_at=_parse_dt(row.get("created_at")),
        book_id=row.get("book_id"),
        session_id=row.get("session_id"),
        error=row.get("error"),
        cache_hit=bool(row.get("cache_hit", False)),
        qa=row.get("qa"),
        director_edits=tuple(row.get("director_edits", ()) or ()),
    )


def load_dump(path: str) -> list[RawTrace]:
    """Read a JSONL trace dump into raw traces."""
    rows: list[RawTrace] = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            rows.append(raw_from_row(json.loads(line), ordinal=i))
    return rows


def demo_source(n: int) -> InMemoryTraceSource:
    """A synthetic corpus for hermetic demos (no file needed)."""
    from datetime import timedelta

    base = datetime(2026, 1, 1, tzinfo=UTC)
    src = InMemoryTraceSource()
    for i in range(n):
        passed = i % 3 != 0
        src.add(
            RawTrace(
                trace_id=f"t{i}",
                prompt_key="adapter@v3" if i % 2 else "critic.qa",
                prompt_version="3.0.0",
                model="qwen-plus",
                inputs={"page_text": f"reach me user{i}@mail.com beat {i}"},
                output='{"beats":[1]}' if i % 2 else '{"verdict":"pass"}',
                created_at=base + timedelta(minutes=i),
                book_id=f"bk{i % 8}",
                session_id=f"s{i % 5}",
                qa={
                    "verdict": "pass" if passed else "fail",
                    "score": 0.9 if passed else 0.2,
                    "ccs": 0.91 if passed else 0.4,
                },
                director_edits=[{"instruction": "make coat crimson"}] if i % 7 == 0 else (),
            )
        )
    return src


def _source(args: argparse.Namespace) -> InMemoryTraceSource:
    if args.demo:
        return demo_source(args.demo)
    src = InMemoryTraceSource()
    src.extend(load_dump(args.input))
    return src


def _build_config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        do_scrub=not args.no_scrub,
        do_dedup=not args.no_dedup,
        near_dedup=not args.no_near_dedup,
        do_label=not args.no_label,
        do_split=not args.no_split,
        split=SplitConfig(
            ratios=SplitRatios(args.train, args.val, args.test), seed=args.seed
        ),
    )


def cmd_build(args: argparse.Namespace) -> dict[str, Any]:
    svc = DatasetService()
    res = svc.build(args.name, _source(args), config=_build_config(args))
    return res.to_dict()


def cmd_export(args: argparse.Namespace) -> str:
    svc = DatasetService()
    svc.build(args.name, _source(args), config=_build_config(args))
    split = None if args.split == "all" else Split(args.split)
    if args.format == "csv":
        return svc.export_csv(args.name, split=split)
    return svc.export_jsonl(args.name, shape=ExportShape(args.shape), split=split)


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    svc = DatasetService()
    svc.build(args.name, _source(args), config=_build_config(args))
    return svc.build_summary(args.name)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", default="dataset", help="dataset name")
    p.add_argument("--input", help="JSONL trace dump path")
    p.add_argument("--demo", type=int, default=0, help="synthesise N demo traces instead")
    p.add_argument("--no-scrub", action="store_true")
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--no-near-dedup", action="store_true")
    p.add_argument("--no-label", action="store_true")
    p.add_argument("--no-split", action="store_true")
    p.add_argument("--train", type=float, default=0.8)
    p.add_argument("--val", type=float, default=0.1)
    p.add_argument("--test", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=1729)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.mlplatform.datasets.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build a versioned dataset from a trace dump")
    _add_common(b)

    e = sub.add_parser("export", help="build then export a split")
    _add_common(e)
    e.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    e.add_argument(
        "--shape", choices=[s.value for s in ExportShape], default=ExportShape.RECORD.value
    )
    e.add_argument(
        "--split", choices=("all", "train", "val", "test"), default="all"
    )

    i = sub.add_parser("inspect", help="build then print lineage + stats")
    _add_common(i)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.demo and not args.input:
        parser.error("one of --input or --demo is required")

    if args.command == "export":
        out: str | dict[str, Any] = cmd_export(args)
        sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
        return 0

    result = cmd_build(args) if args.command == "build" else cmd_inspect(args)
    sys.stdout.write(json.dumps(result, indent=2))
    sys.stdout.write("\n")
    return 0


def _iter_lines(text: str) -> Iterable[str]:  # pragma: no cover - tiny helper
    yield from text.splitlines()


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())


__all__ = ["build_parser", "demo_source", "load_dump", "main", "raw_from_row"]
