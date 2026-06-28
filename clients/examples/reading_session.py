#!/usr/bin/env python3
"""Example: the whole reading-session loop with the sync Python SDK.

    login -> list books -> open a session -> post intent -> stream events.

Defaults to a built-in httpx MockTransport (no live backend, zero video spend).
Set KINORA_BASE_URL to run against a real backend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

# Allow running from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python" / "src"))

from kinora import KinoraClient  # noqa: E402

BASE_URL = os.environ.get("KINORA_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("KINORA_EMAIL", "demo@kinora.local")
PASSWORD = os.environ.get("KINORA_PASSWORD", "demo-password-123")
USE_MOCK = "KINORA_BASE_URL" not in os.environ

_SSE = (
    ": connected\n\n"
    'event: buffer_state\ndata: {"event":"buffer_state","committed_seconds_ahead":25,'
    '"bursting":true,"idle":false,"budget_remaining_s":1650}\n\n'
    'event: clip_ready\ndata: {"event":"clip_ready","shot_id":"shot_0001",'
    '"oss_url":"http://example/clip.mp4","video_seconds":0}\n\n'
)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/api/auth/login"):
        return httpx.Response(200, json={"access_token": "demo", "token_type": "bearer", "expires_in": 3600})
    if url.endswith("/api/books"):
        return httpx.Response(200, json=[{"id": "book_demo", "title": "The Demo Book", "status": "ready", "progress": 1.0}])
    if url.endswith("/api/sessions"):
        return httpx.Response(201, json={"session_id": "sess_demo", "book_id": "book_demo", "focus_word": 0, "velocity_wps": 4.0, "mode": "viewer", "committed_seconds_ahead": 0.0})
    if "/intent" in url:
        return httpx.Response(200, json={"session_id": "sess_demo", "settled": True, "committed_seconds_ahead": 25.0, "promoted": ["shot_0001"], "keyframed": ["shot_0002"]})
    if "/events" in url:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_SSE)
    return httpx.Response(404, json={"error": {"type": "not_found", "message": url}})


def main() -> None:
    print(f"Kinora Python example — {'MOCK backend' if USE_MOCK else BASE_URL}")
    http = httpx.Client(transport=httpx.MockTransport(_mock_handler)) if USE_MOCK else None
    with KinoraClient(BASE_URL, http_client=http) as client:
        client.auth.login(EMAIL, PASSWORD)
        print("authenticated:", client.is_authenticated())

        book = next((b for b in client.books.list() if b.status == "ready"), None)
        if book is None:
            raise SystemExit("no ready book — run `make seed-demo`")
        print(f"book: {book.title} ({book.id}) status={book.status}")

        session = client.sessions.create(book.id, focus_word=0)
        print(f"session: {session.session_id}")

        intent = client.sessions.intent(session.session_id, focus_word=120, velocity=4.2)
        print(f"intent -> promoted={intent.promoted} ahead={intent.committed_seconds_ahead}s")

        print("streaming events…")
        for event in client.sessions.iter_events(session.session_id):
            if event.name == "buffer_state":
                print(f"  buffer_state: {event['committed_seconds_ahead']}s ahead")
            elif event.name == "clip_ready":
                print(f"  clip_ready: shot {event['shot_id']} -> {event['oss_url']}")
                break
    print("done.")


if __name__ == "__main__":
    main()
