"""A standalone operator CLI for the streaming log (``python -m app.streaming.log.cli``).

Self-contained admin tooling that builds a :class:`~app.streaming.log.redis.
RedisStreamsBroker` from a Redis URL and exposes the operational queries an
operator needs: list/describe topics, inspect a consumer group's lag, and tail a
partition. It deliberately does **not** hook into the project's `kinora-admin`
tree (that would couple this facet to the full composition root); keeping it here
means the log package stays importable and operable on its own.

Output is plain text by default, ``--json`` for machine consumption. The command
handlers are thin wrappers over :class:`~app.streaming.log.admin.Admin`, so the
CLI has no logic the library doesn't already expose + test.

Usage::

    python -m app.streaming.log.cli --url redis://localhost:6379/0 topics
    python -m app.streaming.log.cli describe beats
    python -m app.streaming.log.cli lag renderers beats
    python -m app.streaming.log.cli tail beats 0 --max 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from app.streaming.log.admin import Admin
from app.streaming.log.errors import StreamingError
from app.streaming.log.redis import RedisStreamAdapter, RedisStreamsBroker

__all__ = ["build_parser", "main", "run"]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser tree."""
    parser = argparse.ArgumentParser(
        prog="app.streaming.log.cli",
        description="Operator CLI for the Kinora streaming log.",
    )
    parser.add_argument(
        "--url",
        default="redis://localhost:6379/0",
        help="Redis URL (default: redis://localhost:6379/0)",
    )
    parser.add_argument(
        "--namespace", default="kinora:stream", help="Broker key namespace."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("topics", help="List all topics.")

    p_describe = sub.add_parser("describe", help="Describe a topic (config + offsets).")
    p_describe.add_argument("topic")

    p_lag = sub.add_parser("lag", help="Show a consumer group's lag on a topic.")
    p_lag.add_argument("group")
    p_lag.add_argument("topic")

    p_tail = sub.add_parser("tail", help="Print recent records from a partition.")
    p_tail.add_argument("topic")
    p_tail.add_argument("partition", type=int)
    p_tail.add_argument("--max", type=int, default=20, help="Max records to print.")

    return parser


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the selected command; return a JSON-serializable result dict."""
    adapter = RedisStreamAdapter.from_url(args.url)
    broker = RedisStreamsBroker(adapter, namespace=args.namespace)
    await broker.start()
    admin = Admin(broker)
    try:
        return await _dispatch(broker, admin, args)
    finally:
        await adapter.aclose()


async def _dispatch(
    broker: RedisStreamsBroker, admin: Admin, args: argparse.Namespace
) -> dict[str, Any]:
    if args.command == "topics":
        return {"topics": list(await broker.topics())}

    if args.command == "describe":
        desc = await admin.describe(args.topic)
        return {
            "topic": desc.config.name,
            "partitions": desc.config.partitions,
            "cleanup_policy": str(desc.config.cleanup_policy),
            "total_records": desc.total_records,
            "offsets": [
                {
                    "partition": p.partition,
                    "log_start_offset": p.log_start_offset,
                    "log_end_offset": p.log_end_offset,
                    "records": p.record_count,
                }
                for p in desc.partitions
            ],
        }

    if args.command == "lag":
        lag = await admin.group_lag(args.group, args.topic)
        return {
            "group": lag.group_id,
            "generation": lag.generation,
            "total_lag": lag.total,
            "per_partition": {str(tp): n for tp, n in lag.per_partition.items()},
        }

    if args.command == "tail":
        from app.streaming.log.record import TopicPartition

        tp = TopicPartition(args.topic, args.partition)
        ends = await broker.end_offsets((tp,))
        starts = await broker.beginning_offsets((tp,))
        # Start `--max` records back from the head (clamped to the log start).
        offset = max(starts[tp], ends[tp] - args.max)
        result = await broker.fetch(args.topic, args.partition, offset, max_records=args.max)
        return {
            "topic": args.topic,
            "partition": args.partition,
            "high_watermark": result.high_watermark,
            "records": [
                {
                    "offset": r.offset,
                    "timestamp_ms": r.timestamp_ms,
                    "key": r.key_str(),
                    "value": r.value_str() if r.value is not None else None,
                }
                for r in result.records
            ],
        }

    raise StreamingError(f"unknown command {args.command!r}")  # pragma: no cover


def _render(result: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(result, indent=2)
    lines: list[str] = []
    for key, value in result.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except StreamingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_render(result, as_json=args.json))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
