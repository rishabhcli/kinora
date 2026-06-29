"""Redis-Streams-backed broker implementation (production / multi-process).

:class:`~app.streaming.log.redis.broker.RedisStreamsBroker` implements the
:class:`~app.streaming.log.broker.Broker` protocol over Redis: one **Redis
Stream** per ``(topic, partition)`` is the durable append log, with topic
configuration, idempotence sequences, transaction buffers, and a group's
committed offsets stored in Redis hashes. Records are addressed by a dense
integer offset the broker assigns (an ``INCR`` counter), independent of Redis'
own stream entry ids, so the offset semantics match the in-memory broker exactly.

The broker talks to Redis through a tiny typed seam,
:class:`~app.streaming.log.redis.client.StreamRedis`, with two implementations:

* a thin adapter over ``redis.asyncio`` for production, and
* :class:`~app.streaming.log.redis.client.FakeStreamRedis`, an in-process double
  modelling exactly the stream/hash/string commands the broker uses — so the
  Redis broker's *logic* is unit-tested with **zero infra**, and the live
  integration test (gated on ``KINORA_TEST_REDIS_URL``) only confirms the
  adapter wiring.
"""

from __future__ import annotations

from app.streaming.log.redis.broker import RedisStreamsBroker
from app.streaming.log.redis.client import (
    FakeStreamRedis,
    RedisStreamAdapter,
    StreamRedis,
)

__all__ = [
    "FakeStreamRedis",
    "RedisStreamAdapter",
    "RedisStreamsBroker",
    "StreamRedis",
]
