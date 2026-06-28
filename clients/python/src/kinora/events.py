"""Typed Server-Sent-Events for the Kinora Python SDK.

The backend streams session/library events as SSE frames
(``event: <name>\\ndata: <json>\\n\\n``, plus ``:``-prefixed keepalive comments).
This module turns a byte/line stream into typed :class:`Event` objects.

A :class:`SseDecoder` does the incremental framing (so it works over chunked
``httpx`` byte iterators, sync or async), and :func:`parse_event` projects a raw
frame into the right typed event. The clients expose ``iter_events`` /
``aiter_events`` built on top.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Event:
    """A decoded SSE event. ``name`` is the event type; ``data`` is its JSON payload."""

    name: str
    data: Json = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass(frozen=True, slots=True)
class RawFrame:
    """A raw SSE frame before JSON parsing."""

    event: str
    data: str
    id: str | None = None


class SseDecoder:
    """Incremental SSE framing over a text stream.

    Feed it decoded text chunks; it yields complete :class:`RawFrame` objects as
    blank-line-delimited frames become available. Tolerates ``\\n\\n`` and
    ``\\r\\n\\r\\n`` separators and ``:`` comment/keepalive lines.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[RawFrame]:
        """Append a chunk and return any newly-complete frames."""
        self._buffer += chunk
        frames: list[RawFrame] = []
        while True:
            idx, boundary_len = _frame_boundary(self._buffer)
            if idx == -1:
                break
            block = self._buffer[:idx]
            self._buffer = self._buffer[idx + boundary_len :]
            frame = _parse_block(block)
            if frame is not None:
                frames.append(frame)
        return frames

    def flush(self) -> RawFrame | None:
        """Return a trailing frame missing its final blank line (stream end)."""
        block = self._buffer
        self._buffer = ""
        return _parse_block(block) if block.strip() else None


def _frame_boundary(s: str) -> tuple[int, int]:
    a = s.find("\n\n")
    b = s.find("\r\n\r\n")
    if a == -1 and b == -1:
        return -1, 0
    if a == -1:
        return b, 4
    if b == -1:
        return a, 2
    return (a, 2) if a < b else (b, 4)


def _parse_block(block: str) -> RawFrame | None:
    event = "message"
    data_lines: list[str] = []
    id_: str | None = None
    saw_field = False
    for line in block.replace("\r\n", "\n").split("\n"):
        if line.startswith(":"):
            continue  # comment / keepalive
        if ":" in line:
            name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            name, value = line, ""
        if name == "event":
            event = value
            saw_field = True
        elif name == "data":
            data_lines.append(value)
            saw_field = True
        elif name == "id":
            id_ = value
            saw_field = True
    if not saw_field or not data_lines:
        return None
    return RawFrame(event=event, data="\n".join(data_lines), id=id_)


def parse_event(frame: RawFrame) -> Event | None:
    """Project a raw frame into a typed :class:`Event` (None for non-JSON data)."""
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    # Prefer the SSE event name; else trust the payload's own ``event`` field.
    name = frame.event if frame.event and frame.event != "message" else str(payload.get("event", "message"))
    return Event(name=name, data=payload)


def decode_text_stream(chunks: Iterable[str]) -> Iterator[Event]:
    """Decode a synchronous iterable of text chunks into typed events."""
    decoder = SseDecoder()
    for chunk in chunks:
        for frame in decoder.feed(chunk):
            event = parse_event(frame)
            if event is not None:
                yield event
    tail = decoder.flush()
    if tail is not None:
        event = parse_event(tail)
        if event is not None:
            yield event


# Canonical event names (mirrors clients/spec EVENTS).
EVENT_NAMES = (
    "buffer_state",
    "clip_ready",
    "keyframe_ready",
    "scene_stitched",
    "event_stitched",
    "agent_activity",
    "regen_done",
    "budget_low",
    "conflict_choice",
    "ingest_progress",
)

__all__ = [
    "EVENT_NAMES",
    "Event",
    "RawFrame",
    "SseDecoder",
    "decode_text_stream",
    "parse_event",
]
