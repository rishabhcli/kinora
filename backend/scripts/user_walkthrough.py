#!/usr/bin/env python3
"""Simulated user walkthrough against a running Kinora API (manual QA script)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:8000"
EMAIL = "e2e@kinora.test"
PASSWORD = "e2e-password-123"
PDF = Path(__file__).resolve().parents[2] / "assets" / "books" / "little_red_riding_hood.pdf"


def main() -> int:
    notes: list[str] = []
    with httpx.Client(base_url=API, timeout=30.0) as http:
        login = http.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if login.status_code != 200:
            print("FAIL login", login.status_code, login.text)
            return 1
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("OK login")

        books = http.get("/api/books", headers=headers).json()
        print(f"OK shelf ({len(books)} books)")
        ready = [b for b in books if b["status"] == "ready"]
        if not ready:
            notes.append("Shelf has no ready books after login — first-run experience is empty.")
        else:
            print(f"  ready: {ready[0]['title']}")

        if not PDF.exists():
            print(f"SKIP upload — missing {PDF}")
        else:
            up = http.post(
                "/api/books",
                headers=headers,
                files={"file": (PDF.name, PDF.read_bytes(), "application/pdf")},
                data={"title": "Little Red Riding Hood", "art_direction": "woodcut fairy tale"},
            )
            if up.status_code not in (200, 201):
                notes.append(f"Upload failed unexpectedly: {up.status_code} {up.text[:200]}")
                print("FAIL upload", up.status_code, up.text[:200])
            else:
                book = up.json()
                print(f"OK upload -> status={book['status']} id={book['id']}")
                if book["status"] != "importing":
                    notes.append("Upload did not return importing status — shelf progress UX won't trigger.")

                bad = http.post(
                    "/api/books",
                    headers=headers,
                    files={"file": ("notes.txt", b"not a book", "text/plain")},
                )
                if bad.status_code == 415:
                    print("OK upload rejects unsupported type with 415")
                else:
                    notes.append(f"Unsupported upload should 415, got {bad.status_code}")

                if ready:
                    session = http.post(
                        "/api/sessions",
                        headers=headers,
                        json={"book_id": ready[0]["id"], "focus_word": 0, "mode": "viewer"},
                    )
                    if session.status_code == 200:
                        sid = session.json()["session_id"]
                        shots = http.get(f"/api/books/{ready[0]['id']}/shots", headers=headers).json()
                        print(f"OK reading room session={sid} shots={len(shots.get('shots', []))}")
                    else:
                        notes.append(f"Could not start session: {session.status_code}")

        demo = http.post("/api/auth/login", json={"email": "demo@kinora.local", "password": "demo-password-123"})
        if demo.status_code != 200:
            notes.append(
                "Desktop demo login (demo@kinora.local) fails unless seed_demo ran — confusing for new users."
            )
            print("NOTE demo login unavailable (expected without seed_demo)")

    print("\n=== QA NOTES ===")
    if not notes:
        print("No issues recorded.")
    else:
        for note in notes:
            print(f"- {note}")
    Path("/tmp/kinora_qa_notes.json").write_text(json.dumps(notes, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
