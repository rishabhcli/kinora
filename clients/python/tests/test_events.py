"""SSE decoding + streaming. All HTTP mocked via respx."""

from __future__ import annotations

import json

import httpx
import respx

from kinora import KinoraClient, SseDecoder, decode_text_stream, parse_event
from kinora.events import RawFrame

from conftest import BASE_URL


def _frame(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_decoder_handles_chunk_boundaries() -> None:
    decoder = SseDecoder()
    full = _frame("clip_ready", {"shot_id": "s1", "oss_url": "x"})
    mid = len(full) // 2
    assert decoder.feed(full[:mid]) == []  # incomplete
    frames = decoder.feed(full[mid:])
    assert len(frames) == 1
    assert frames[0].event == "clip_ready"


def test_decoder_skips_keepalive_comments() -> None:
    decoder = SseDecoder()
    frames = decoder.feed(": connected\n\n: keepalive\n\n")
    assert frames == []


def test_decoder_multiple_frames_one_chunk() -> None:
    decoder = SseDecoder()
    chunk = _frame("buffer_state", {"committed_seconds_ahead": 30}) + _frame("clip_ready", {"shot_id": "s1"})
    frames = decoder.feed(chunk)
    assert [f.event for f in frames] == ["buffer_state", "clip_ready"]


def test_decoder_flush_trailing_frame() -> None:
    decoder = SseDecoder()
    assert decoder.feed("event: budget_low\ndata: {\"budget_remaining_s\": 10}") == []
    tail = decoder.flush()
    assert tail is not None
    assert tail.event == "budget_low"


def test_parse_event_falls_back_to_payload_event() -> None:
    ev = parse_event(RawFrame(event="message", data='{"event": "regen_done", "shot_id": "s2"}'))
    assert ev is not None
    assert ev.name == "regen_done"
    assert ev["shot_id"] == "s2"


def test_parse_event_non_json_returns_none() -> None:
    assert parse_event(RawFrame(event="x", data="not json")) is None


def test_decode_text_stream() -> None:
    chunks = [": connected\n\n", _frame("clip_ready", {"shot_id": "s1"}), _frame("buffer_state", {"committed_seconds_ahead": 25})]
    events = list(decode_text_stream(chunks))
    assert [e.name for e in events] == ["clip_ready", "buffer_state"]


@respx.mock
def test_iter_events_streams_typed_events(client: KinoraClient) -> None:
    client.token = "tok"
    body = (
        ": connected\n\n"
        + _frame("buffer_state", {"committed_seconds_ahead": 25, "bursting": False, "idle": True, "budget_remaining_s": None})
        + _frame("clip_ready", {"shot_id": "s1", "oss_url": "http://x/c.mp4", "video_seconds": 0})
    )
    route = respx.get(f"{BASE_URL}/api/sessions/s1/events").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    )
    received = [ev.name for ev in client.sessions.iter_events("s1")]
    assert received == ["buffer_state", "clip_ready"]
    # bearer header, not query, by default
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    assert "token=" not in str(route.calls.last.request.url)


@respx.mock
def test_iter_events_token_in_query(client: KinoraClient) -> None:
    client.token = "tok"
    route = respx.get(f"{BASE_URL}/api/sessions/s1/events").mock(
        return_value=httpx.Response(200, text=_frame("clip_ready", {"shot_id": "s1"}))
    )
    list(client.sessions.iter_events("s1", token_in_query=True))
    assert "token=tok" in str(route.calls.last.request.url)
