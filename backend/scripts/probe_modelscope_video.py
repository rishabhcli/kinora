#!/usr/bin/env python3
"""One-off diagnostic: discover ModelScope's real video-generation API contract.

Not part of the app. Run manually, once, with a real MODELSCOPE_API_TOKEN, to
confirm the request/response shape before backend/app/providers/modelscope.py
is written against it. Delete or archive after Task 2 is confirmed.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

TOKEN = os.environ.get("MODELSCOPE_API_TOKEN")
BASE = "https://api-inference.modelscope.cn/v1"


def probe() -> int:
    if not TOKEN:
        print("MODELSCOPE_API_TOKEN not set — nothing to probe yet.", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {TOKEN}"}

    # Candidate video endpoints, cheapest/safest first (a 404/405 tells us the
    # real path faster than a successful-but-wrong-shape 200 would).
    candidates = [
        ("POST", "/videos/generations"),
        ("POST", "/video/generations"),
        ("POST", "/images/generations"),  # confirmed-real async pattern, for comparison
    ]
    with httpx.Client(base_url=BASE, headers=headers, timeout=30.0) as c:
        for method, path in candidates:
            try:
                r = c.request(method, path, json={"model": "probe", "prompt": "probe"})
                print(f"{method} {path} -> {r.status_code}")
                print(json.dumps(r.json(), indent=2)[:2000] if r.content else "(empty)")
            except Exception as e:  # noqa: BLE001 - diagnostic script, report and continue
                print(f"{method} {path} -> ERROR: {e}")
            print("---")
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())
