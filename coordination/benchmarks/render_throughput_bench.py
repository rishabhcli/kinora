"""Render-throughput benchmark: the REAL ffmpeg Ken-Burns degrade lane.

Serial (one shot at a time) vs bounded-concurrent via app.optim.batch.gather_bounded over a thread
pool (ffmpeg's subprocess releases the GIL). Demonstrates the throughput available from per-lane
parallelism (the patch proposed to Agent 1). Uses degrade.ken_burns_over_image — production code.
"""

from __future__ import annotations

import asyncio
import io
import time

from PIL import Image

from app.optim.batch import gather_bounded
from app.render.degrade import ken_burns_over_image, probe

N = 8
DURATION_S = 3.0
LIMIT = 4


def make_still() -> bytes:
    img = Image.new("RGB", (720, 1280))
    px = img.load()
    for y in range(1280):
        for x in range(0, 720, 4):  # coarse gradient, cheap to build
            v = (x + y) % 256
            for dx in range(4):
                if x + dx < 720:
                    px[x + dx, y] = (v, (v * 2) % 256, 255 - v)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def main() -> None:
    still = make_still()
    # warm-up (resolve ffmpeg, prime caches) — not timed
    out = ken_burns_over_image(still, DURATION_S)
    info = probe(out)
    print(f"clip: {len(out)//1024} KB, {info.width}x{info.height}, ~{info.duration_s:.1f}s "
          f"(verify ok); N={N}, duration={DURATION_S}s, concurrency limit={LIMIT}")

    t0 = time.perf_counter()
    for _ in range(N):
        ken_burns_over_image(still, DURATION_S)
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    await gather_bounded(
        [asyncio.to_thread(ken_burns_over_image, still, DURATION_S) for _ in range(N)],
        limit=LIMIT,
    )
    concurrent = time.perf_counter() - t0

    print(f"\nSerial      : {serial:6.2f}s total  -> {N/serial:5.2f} clips/s")
    print(f"Concurrent  : {concurrent:6.2f}s total  -> {N/concurrent:5.2f} clips/s  "
          f"({serial/concurrent:.2f}x throughput at limit={LIMIT})")


asyncio.run(main())
