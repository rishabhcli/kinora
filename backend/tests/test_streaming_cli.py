"""CLI tests — parser tree, rendering, and (gated) live end-to-end."""

from __future__ import annotations

import os
import uuid

import pytest

from app.streaming.log import cli
from app.streaming.log.producer import Producer
from app.streaming.log.record import ProducerRecord
from app.streaming.log.redis import RedisStreamAdapter, RedisStreamsBroker
from app.streaming.log.topic import TopicConfig

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")


def test_parser_requires_a_subcommand() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])  # no subcommand → argparse exits


def test_parser_parses_each_command() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["topics"]).command == "topics"
    assert parser.parse_args(["describe", "beats"]).topic == "beats"
    lag = parser.parse_args(["lag", "g", "beats"])
    assert (lag.group, lag.topic) == ("g", "beats")
    tail = parser.parse_args(["tail", "beats", "1", "--max", "5"])
    assert (tail.topic, tail.partition, tail.max) == ("beats", 1, 5)


def test_render_text_and_json() -> None:
    result = {"topics": ["a", "b"], "count": 2}
    text = cli._render(result, as_json=False)
    assert "topics:" in text
    assert "- a" in text
    assert "count: 2" in text
    parsed = cli._render(result, as_json=True)
    import json

    assert json.loads(parsed) == result


@pytest.mark.skipif(not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set")
async def test_cli_end_to_end_against_live_redis() -> None:
    assert _REDIS_URL is not None
    ns = f"kinora:test:cli:{uuid.uuid4().hex[:8]}"
    adapter = RedisStreamAdapter.from_url(_REDIS_URL)
    broker = RedisStreamsBroker(adapter, namespace=ns)
    await broker.start()
    await broker.create_topic(TopicConfig.deleted("beats", partitions=2))
    producer = Producer(broker)
    for i in range(4):
        await producer.send_and_wait(
            ProducerRecord.from_str("beats", f"v{i}", key="k", partition=0)
        )
    await producer.close()

    try:
        ns_args = cli.build_parser().parse_args(
            ["--url", _REDIS_URL, "--namespace", ns, "describe", "beats"]
        )
        result = await cli.run(ns_args)
        assert result["topic"] == "beats"
        assert result["total_records"] == 4

        tail_args = cli.build_parser().parse_args(
            ["--url", _REDIS_URL, "--namespace", ns, "tail", "beats", "0", "--max", "2"]
        )
        tail = await cli.run(tail_args)
        assert [r["value"] for r in tail["records"]] == ["v2", "v3"]
    finally:
        keys = await adapter.keys(f"{ns}:*")
        if keys:
            await adapter.delete(*keys)
        await adapter.aclose()
