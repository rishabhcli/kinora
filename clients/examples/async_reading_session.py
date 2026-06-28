#!/usr/bin/env python3
"""Example: the reading-session loop on the async Python client.

Defaults to a built-in httpx MockTransport (no live backend, zero video spend).
Set KINORA_BASE_URL to run against a real backend.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python" / "src"))

from kinora import AsyncKinoraClient  # noqa: E402

BASE_URL = os.environ.get("KINORA_BASE_URL", "http://localhost:8000")
USE_MOCK = "KINORA_BASE_URL" not in os.environ

_SSE = (
    'event: buffer_state\ndata: {"event":"buffer_state","committed_seconds_ahead":30,'
    '"bursting":true,"idle":false,"budget_remaining_s":1650}\n\n'
    'event: clip_ready\ndata: {"event":"clip_ready","shot_id":"shot_0001",'
    '"oss_url":"http://example/clip.mp4"}\n\n'
)


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/api/auth/login"):
        return httpx.Response(200, json={"access_token": "demo", "token_type": "bearer", "expires_in": 3600})
    if url.endswith("/api/sessions"):
        return httpx.Response(201, json={"session_id": "sess_demo", "book_id": "book_demo", "focus_word": 0, "velocity_wps": 4.0, "mode": "viewer", "committed_seconds_ahead": 0.0})
    if "/events" in url:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_SSE)
    return httpx.Response(404, json={"error": {"type": "not_found", "message": url}})


async def main() -> None:
    print(f"Kinora async Python example — {'MOCK backend' if USE_MOCK else BASE_URL}")
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler)) if USE_MOCK else None
    async with AsyncKinoraClient(BASE_URL, http_client=http) as client:
        await client.auth.login("demo@kinora.local", "demo-password-123")
        session = await client.sessions.create("book_demo", focus_word=0)
        print(f"session: {session.session_id}")
        async for event in client.sessions.iter_events(session.session_id):
            if event.name == "buffer_state":
                print(f"  buffer_state: {event['committed_seconds_ahead']}s ahead")
            elif event.name == "clip_ready":
                print(f"  clip_ready: {event['oss_url']}")
                break
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
