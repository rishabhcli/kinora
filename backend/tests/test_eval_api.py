"""Eval API contract tests — the EXACT shapes the Phase-10 frontend depends on.

Driven through the Phase-9 app over httpx ``ASGITransport`` with the throwaway
Postgres/Redis/MinIO container fixtures. Covers the live-recomputed buffer-trace
sawtooth and the cached crew-vs-baseline report, plus ownership 404s.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.api.routes.metrics import report_cache_key
from app.composition import Container
from app.db.models.enums import ShotStatus
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from tests.conftest import register_login, seed_owned_book


async def _seed_shots_and_spans(
    container: Container, book_id: str, *, count: int = 80, spacing: int = 10
) -> None:
    async with container.session_factory() as session:
        shots = ShotRepo(session)
        spans = SourceSpanRepo(session)
        for i in range(1, count + 1):
            start = i * spacing
            await shots.create(
                id=f"shot_{i:04d}",
                book_id=book_id,
                beat_id=f"beat_{i:04d}",
                scene_id="scene_1",
                status=ShotStatus.PLANNED,
                duration_s=5.0,
                source_span={"word_range": [start, start + spacing]},
            )
        await spans.bulk_insert(
            [
                {
                    "book_id": book_id,
                    "word_index_start": i * spacing,
                    "word_index_end": i * spacing + spacing,
                    "shot_id": f"shot_{i:04d}",
                    "beat_id": f"beat_{i:04d}",
                    "scene_id": "scene_1",
                }
                for i in range(1, count + 1)
            ]
        )


async def _create_session(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    resp = await client.post("/api/sessions", headers=headers, json={"book_id": book_id})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_id"])


def _sample_report(book_id: str) -> dict[str, object]:
    return {
        "ccs": {"crew": 0.94, "baseline": 0.71},
        "efficiency": {"crew": 92.0, "baseline": 64.0},
        "regen_rate": {"crew": 0.08, "baseline": 0.33},
        "style_drift": {"crew": 0.02, "baseline": 0.11},
        "runs": 3,
        "thresholds": {"ccs_min": 0.85, "style_drift_max": 0.08, "motion_artifact_max": 0.25},
        "per_character_ccs": {"crew": {"char_a": 0.94}, "baseline": {"char_a": 0.71}},
    }


# --------------------------------------------------------------------------- #
# GET /api/eval/buffer-trace/{session_id}
# --------------------------------------------------------------------------- #


async def test_buffer_trace_endpoint_returns_sawtooth_contract(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shots_and_spans(container, book_id)
    session_id = await _create_session(api_client, auth_headers, book_id)

    resp = await api_client.get(
        f"/api/eval/buffer-trace/{session_id}",
        headers=auth_headers,
        params={"velocity": 4.0, "duration_s": 90.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The EXACT shared contract: a JSON array of {t, committed_seconds_ahead, low, high}.
    assert isinstance(body, list) and len(body) > 5
    for item in body:
        assert set(item.keys()) == {"t", "committed_seconds_ahead", "low", "high"}

    times = [p["t"] for p in body]
    occupancy = [p["committed_seconds_ahead"] for p in body]
    assert times == sorted(times) and len(set(times)) == len(times)  # monotonic time
    assert all(p["low"] == container.settings.watermark_low_s for p in body)
    assert all(p["high"] == container.settings.watermark_high_s for p in body)
    assert max(occupancy) == container.settings.watermark_high_s  # filled to H
    assert min(occupancy) >= 0.0
    assert any(b - a > 10.0 for a, b in zip(occupancy, occupancy[1:], strict=False))  # refill burst


async def test_buffer_trace_not_owned_is_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    other = await register_login(api_client, "intruder@example.com")
    resp = await api_client.get(f"/api/eval/buffer-trace/{session_id}", headers=other)
    assert resp.status_code == 404


async def test_buffer_trace_unknown_session_is_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/eval/buffer-trace/sess_missing", headers=auth_headers)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/eval/report/{book_id}
# --------------------------------------------------------------------------- #


async def test_report_endpoint_returns_cached_contract(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    report = _sample_report(book_id)
    await container.redis.set_json(report_cache_key(book_id), report)

    resp = await api_client.get(f"/api/eval/report/{book_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The EXACT shared contract.
    assert set(body["ccs"].keys()) == {"crew", "baseline"}
    assert set(body["efficiency"].keys()) == {"crew", "baseline"}
    assert set(body["regen_rate"].keys()) == {"crew", "baseline"}
    assert set(body["style_drift"].keys()) == {"crew", "baseline"}
    assert body["runs"] == 3
    assert body["thresholds"]["ccs_min"] == 0.85
    assert body["per_character_ccs"]["crew"]["char_a"] == 0.94
    assert body["ccs"]["crew"] > body["ccs"]["baseline"]


async def test_report_not_ready_is_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.get(f"/api/eval/report/{book_id}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "eval_report_not_ready"


async def test_report_not_owned_is_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await container.redis.set_json(report_cache_key(book_id), _sample_report(book_id))
    other = await register_login(api_client, "stranger2@example.com")
    resp = await api_client.get(f"/api/eval/report/{book_id}", headers=other)
    assert resp.status_code == 404
