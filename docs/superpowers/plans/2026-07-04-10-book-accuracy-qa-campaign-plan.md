# 10-Book Story-Accuracy & Video-Sync QA Campaign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote Kinora's already-built-but-dormant multi-shot continuity system into the live render path with a real repair loop, add a new whole-book long-range continuity auditor, stack free/cheap video providers behind the existing (also dormant) `VideoRouter`, and use all of it to meticulously verify + fix story accuracy and video-sync correctness across 10 full real novels, producing durable committable evidence.

**Architecture:** Three accuracy layers stack on the existing live single-shot Critic gate (unchanged): (1) event-level seam-continuity + repair, (2) whole-book long-range continuity audit (new), (3) a multi-provider `VideoRouter` (ModelScope free-tier primary, MiniMax paid gap-filler, $15 hard cap) feeding real video into all of it. The client's existing-but-unused merged-clip seek machinery (`timeline.ts`) gets exercised for the first time via a small adapter change. All new code follows this repo's established provider/agent patterns (`minimax.py`, `continuity_qa.py`) rather than inventing new shapes.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy (async), httpx, pytest, pydantic-settings, TypeScript/React (Vite), Vitest, Playwright.

## Global Constraints

- **NO automatic git commits beyond what each task's own commit step specifies; NEVER `git push`.** The user commits/pushes only on their own explicit instruction outside this plan — see project memory `dont-ask-about-committing`. Each task's commit step stages and commits ONLY that task's files.
- **Real money is at stake.** `KINORA_LIVE_VIDEO` is already `true` and `VIDEO_BACKEND=minimax` in `backend/.env`; a hard, self-imposed **$15 total** MiniMax spend ceiling applies to this entire campaign (code default guard is $30 — this plan tightens it further, see Task 3). Free providers (ModelScope, then HF spot-checks) are tried first via the router; MiniMax is the last resort.
- **No book is truncated.** All 10 books ingest at full text via the live API pipeline (`seed_public_domain.py`'s pattern). Never use `seed_public_domain_direct.py`'s `build_book()` or `seed_library_100.py` for any of the 10 campaign books — that path fabricates synthetic beats/entities and a **hardcoded fake QA verdict** (`qa={"ccs": 0.92, ..., "reason": "seed"}`), not real ingestion or real QA. Verified 2026-07-04.
- **Default behavior must not change for anyone not opted into this campaign.** `render_granularity` defaults to `"shot"` (today's behavior, untouched). The campaign explicitly sets it to `"event"`.
- Test runner: `backend/.venv/bin/pytest tests/<path>::<name> -q` from `backend/`. Lint: `make lint` (ruff + mypy) from the repo root. Desktop: `pnpm --filter @kinora/desktop run typecheck && test && build`.
- Mock HTTP exactly as existing provider tests do: `httpx.MockTransport(handler)` passed to `ProviderClient(...)`. Never make a real network call in a unit test (the one deliberate exception is Task 1's explicit, human-run probe script, which is not a test).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/scripts/probe_modelscope_video.py` (create) | One-off, human-run diagnostic: discover ModelScope's real video-generation endpoint contract empirically. Not part of the app; not a test. |
| `backend/app/providers/modelscope.py` (create) | `ModelScopeVideoProvider` — a `VideoBackend`, mirroring `minimax.py`'s submit→poll→retrieve→download→`record_usage` shape, built against Task 1's confirmed contract. |
| `backend/tests/test_providers_modelscope.py` (create) | Unit tests for the provider (mocked HTTP only). |
| `backend/app/core/config.py` (modify, after L117) | `render_granularity` setting; `modelscope_*` settings. |
| `backend/app/providers/__init__.py` (modify, ~L110-199) | Add the ModelScope branch; assemble a cross-provider `VideoRouter` (ModelScope + MiniMax) as `Providers.video` instead of a single backend. |
| `backend/tests/test_providers_router_assembly.py` (create) | Tests that `create_providers()` assembles the router correctly and orders free-before-paid. |
| `backend/app/render/book_continuity_audit.py` (create) | The new whole-book long-range continuity auditor (pure functions, no ffmpeg). |
| `backend/tests/test_book_continuity_audit.py` (create) | Unit tests for the auditor. |
| `backend/app/render/live_event_renderer.py` (create) | `LiveEventShotRenderer` — an `EventShotRenderer` adapter that calls the live `VideoBackend`/router AND runs the same per-shot Critic gate `RenderPipeline` runs today, before handing a `RenderedShot` back to `EventDirector`. |
| `backend/tests/test_live_event_renderer.py` (create) | Unit tests for the adapter (mocked video backend + mocked Critic). |
| `backend/app/render/event_director.py` (modify) | Make `route_event_continuity`'s output actually trigger repair (`INSERT_SUPPLEMENTAL` / `REGEN_CONTINUATION` / `DEGRADE`) inside `render_event`, instead of only logging. |
| `backend/tests/test_render_event_director.py` (modify — file exists per its own test suite; extend it) | New tests for the repair-loop behavior. |
| `backend/app/scheduler/service.py` (modify, `_fill_committed` ~L408-468) | When `render_granularity="event"`, group a scene's ready shots via `pack_segments` and enqueue one event job per group instead of one shot per enqueue call. |
| `backend/app/queue/worker.py` (modify, `_default_run_shot` ~L271-293) | Branch: an event job builds an `EventScript` (via `plan_segment_script`) and calls `EventDirector.render_event` (with `LiveEventShotRenderer` injected) instead of `RenderPipeline.render_shot`. |
| `backend/tests/test_scheduler_event_granularity.py` (create) | Tests for the Scheduler's grouping branch + regression proof that `"shot"` mode is unchanged. |
| `backend/tests/test_worker_event_dispatch.py` (create) | Tests for the Worker's dispatch branch. |
| `backend/app/db/models/shot.py` (modify — exact fields confirmed in Task 8 Step 1) | Add `clip_start_s: float \| None`, `clip_end_s: float \| None` so a shot that's part of a merged event clip can record its offset within it. |
| `backend/app/api/schemas/shot.py` or equivalent `ShotResponse` schema (modify — exact file confirmed in Task 8 Step 1) | Expose `clip_start_s`/`clip_end_s` on the API response. |
| `apps/desktop/src/reading/ScrollFilmEngine.tsx` (modify, `timelineFromProps` ~L68-111) | When shots carry `clip_start_s`/`clip_end_s`, emit `SegmentInput`s sharing one `src` with real `clipStart`/`clipEnd` instead of always giving each shot its own `src`. |
| `apps/desktop/src/reading/__tests__/timeline.test.ts` (modify — confirmed to already exist 2026-07-04, do NOT create a new file at the top-level `reading/` path) | Unit coverage for the shared-`src` segment case (the underlying `resolvePlayhead`/`segmentTime` logic is already covered — this task proves the *adapter* feeds it correctly). |
| `backend/app/cli/actions/review_export.py` (modify) | Numeric QA/CCS scores, seam-repair actions taken, long-range-audit findings in the exported manifest/script/HTML; a cross-book `index.html`. |
| `backend/tests/test_cli_integration.py` (modify — extend existing) | Tests for the extended export fields. |
| `backend/scripts/seed_public_domain.py` (modify, `BOOKS` list ~L27-32) | Replace the 5-title demo list with the 10 campaign titles (confirmed Gutenberg IDs below). |
| `backend/scripts/qa_campaign_report.py` (create) | Aggregates all 10 books' `manifest.json`s into the cross-book `REPORT.md` + the kinora.md §13 metrics. |
| `qa-runs/2026-07-04-10-book-campaign/` (create, gitignored `clips/` subpaths only) | Campaign artifact root — created by the operational tasks in Part C, not by application code. |

---

## Part A — Foundation

### Task 1: Probe ModelScope's real video API contract (spike, not a unit test)

**Files:**
- Create: `backend/scripts/probe_modelscope_video.py`

**Interfaces:**
- Consumes: a `MODELSCOPE_API_TOKEN` env var (human-supplied whenever available).
- Produces: a written record of the real contract (appended to this plan's Task 2 as the confirmed schema) — this task's job is to make Task 2 possible, not to ship product code.

ModelScope's video-generation endpoint is not publicly documented (verified 2026-07-04 — only an analogous async image-generation pattern is confirmed: `POST /v1/images/generations` with `X-ModelScope-Async-Mode: true` header, returns `task_id`, poll `GET /v1/tasks/{task_id}`). Writing provider code against a guessed schema would violate this plan's TDD discipline (tests must assert real behavior, not invented behavior). This task makes one real, cheap, human-run call to find the real shape.

- [ ] **Step 1: Write the probe script**

```python
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
```

- [ ] **Step 2: Run it**

Run: `MODELSCOPE_API_TOKEN=<token> backend/.venv/bin/python backend/scripts/probe_modelscope_video.py`

Expected: one of the candidate paths returns a 200 with a `task_id`-shaped body (confirming the real endpoint), or all return 404/405 (meaning video generation needs a different discovery path — in that case, fall back to the confirmed-real `/images/generations` async pattern as the closest analog and treat video-specific field names as best-effort, documented explicitly as such in Task 2's docstring).

- [ ] **Step 3: Record the confirmed contract**

Append the real request/response JSON (or the "no dedicated endpoint found, using the image-pattern analog" conclusion) as a comment block at the top of `backend/app/providers/modelscope.py` before writing Task 2 — this is the source of truth Task 2's tests are written against, not this plan's guess.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/probe_modelscope_video.py
git commit -m "chore: add ModelScope video API probe script"
```

---

### Task 2: `ModelScopeVideoProvider`

**Files:**
- Create: `backend/app/providers/modelscope.py`
- Test: `backend/tests/test_providers_modelscope.py`
- Modify: `backend/app/core/config.py` (insert after L117, the end of the MiniMax settings block, before the `# --- Frontier hosted video adapters ---` comment at L119)

**Interfaces:**
- Consumes: `ProviderClient` (existing, `backend/app/providers/base.py`), the confirmed contract from Task 1.
- Produces: `class ModelScopeVideoProvider` satisfying `VideoBackend` (`backend/app/providers/video_router.py:52-68`: `name: str`; `async def render(self, spec: WanSpec) -> VideoResult`; `async def healthy(self) -> bool`) — the exact same shape `MiniMaxVideoProvider` already satisfies, so Task 3's router assembly can treat them identically.

**Step 1: Add config settings.** In `backend/app/core/config.py`, immediately after line 117 (`minimax_cost_per_clip_usd: float = 0.19`) and before the `# --- Frontier hosted video adapters ---` comment at line 119, insert:

```python
    # --- ModelScope (Alibaba open model hub) hosted video provider ---
    # Free recurring daily quota (verified 2026-07-04: ~2,000 calls/day across
    # all models, resets 00:00 UTC+8; the video-specific limit is unconfirmed —
    # see backend/scripts/probe_modelscope_video.py). Primary free-tier video
    # path for the 10-book QA campaign, tried before the paid MiniMax provider.
    modelscope_api_key: str | None = None
    modelscope_base_url: str = "https://api-inference.modelscope.cn/v1"
    modelscope_video_model: str = "Wan-AI/Wan2.2-T2V-A14B"

    # --- Live render granularity (additive; default is today's unchanged behavior) ---
    # "shot": the Scheduler promotes and renders one shot at a time (unchanged).
    # "event": the Scheduler groups a scene's ready shots into packed segments
    # (app.render.segment_packer.pack_segments) and renders each group as one
    # continuous multi-shot event via EventDirector, with seam-continuity
    # scoring and repair (kinora.md's dormant event_director/continuity_qa,
    # promoted live for the 10-book QA campaign).
    render_granularity: str = "shot"  # "shot" | "event"
```

- [ ] **Step 1a: Write the config test**

Create `backend/tests/test_providers_modelscope.py` (config assertions first, provider class follows in the next steps of this same file):

```python
"""Unit tests for the ModelScope video provider: config defaults, gate,
submit-body shape, poll mapping, a mocked full success path. No network."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(dashscope_api_key="test", **overrides)  # type: ignore[arg-type]


def test_modelscope_config_defaults() -> None:
    s = _settings()
    assert s.modelscope_api_key is None
    assert s.modelscope_base_url == "https://api-inference.modelscope.cn/v1"
    assert s.modelscope_video_model == "Wan-AI/Wan2.2-T2V-A14B"
    assert s.render_granularity == "shot"


def test_modelscope_config_overrides() -> None:
    s = _settings(modelscope_api_key="ms-key", render_granularity="event")
    assert s.modelscope_api_key == "ms-key"
    assert s.render_granularity == "event"
```

- [ ] **Step 1b: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_modelscope.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'modelscope_api_key'`.

- [ ] **Step 1c: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_modelscope.py -q`
Expected: PASS (2 passed) once Step 1's config edit lands.

- [ ] **Step 2: Write the provider's failing tests** (append to the same test file — structure mirrors `test_providers_minimax.py` exactly, substituting ModelScope's confirmed contract from Task 1 for MiniMax's)

```python
from app.providers.base import ProviderClient, ResilienceConfig
from app.providers.modelscope import ModelScopeVideoProvider
from app.providers.types import WanMode, WanSpec

_FAST = ResilienceConfig(
    max_attempts=2, backoff_base_s=0.0, backoff_max_s=0.0, backoff_jitter_s=0.0,
    breaker_failure_threshold=3, breaker_recovery_s=0.05,
    rate_per_s=1000.0, rate_burst=1000,
)


def _ms_settings(*, live: bool) -> Settings:
    return Settings(
        dashscope_api_key="test", kinora_live_video=live,
        modelscope_api_key="ms-key",
    )


def _ms_client(handler: object, *, live: bool) -> ProviderClient:
    return ProviderClient(
        _ms_settings(live=live),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resilience=_FAST,
        base_url_override="https://api-inference.modelscope.cn/v1",
        api_key_override="ms-key",
    )


async def test_render_raises_when_live_video_disabled() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ModelScope must NOT be called when the gate is off")

    client = _ms_client(_tripwire, live=False)
    provider = ModelScopeVideoProvider(client)
    from app.providers.errors import LiveVideoDisabled

    with pytest.raises(LiveVideoDisabled):
        await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    await client.aclose()


async def test_healthy_is_true_without_network_when_gate_off() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("healthy() must not call the network when gated off")

    client = _ms_client(_tripwire, live=False)
    provider = ModelScopeVideoProvider(client)
    assert await provider.healthy() is True
    await client.aclose()

# NOTE: the submit/poll/retrieve success-path test is written using the EXACT
# request/response shape confirmed by Task 1's probe script — do not write it
# from this plan's assumption. If Task 1 found no dedicated video endpoint,
# write this test against the confirmed image-generation-analog shape
# (POST {path} with X-ModelScope-Async-Mode: true -> {"task_id": ...}; GET
# /tasks/{task_id} -> {"task_status": ..., "output_images"/"output_videos": [...]})
# and mark the model-id/field-name choices with a comment citing that the
# video-specific contract is unconfirmed.
```

- [ ] **Step 3: Run to verify these fail**

Run: `backend/.venv/bin/pytest tests/test_providers_modelscope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.providers.modelscope'`.

- [ ] **Step 4: Write the minimal implementation**

Create `backend/app/providers/modelscope.py`, structured identically to `backend/app/providers/minimax.py` (gate check → submit → poll → retrieve → download → `record_usage`), with the submit/poll/retrieve methods built against Task 1's confirmed contract (or the documented image-pattern analog if no dedicated endpoint was found — in which case the docstring must say so explicitly, per this plan's No Placeholders rule: an honestly-flagged assumption is not a placeholder, a silent guess is). Reuse `LiveVideoDisabled` from `app.providers.errors` for the gate, exactly as `MiniMaxVideoProvider.render` does.

- [ ] **Step 5: Run to verify they pass**

Run: `backend/.venv/bin/pytest tests/test_providers_modelscope.py -q`
Expected: PASS.

- [ ] **Step 6: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_providers_modelscope.py tests/test_providers_minimax.py -q` → confirm PASS (MiniMax's tests must stay green).
Run: `make lint` → confirm PASS for the two new/modified files.

- [ ] **Step 7: Commit**

```bash
git add backend/app/providers/modelscope.py backend/tests/test_providers_modelscope.py backend/app/core/config.py
git commit -m "feat(backend): add ModelScope free-tier video provider"
```

---

### Task 3: Assemble a cross-provider `VideoRouter` (ModelScope free-tier first, MiniMax capped gap-filler)

**Files:**
- Modify: `backend/app/providers/__init__.py` (the `create_providers()` function, ~L110-175, and the `Providers` dataclass's `video` field type at ~L89)
- Test: `backend/tests/test_providers_router_assembly.py`

**Interfaces:**
- Consumes: `ModelScopeVideoProvider` (Task 2), `MiniMaxVideoProvider` (existing), `VideoRouter`/`RouterPolicy`/`BackendTier`/`RouteMode` (existing, `backend/app/providers/video_router.py:52-237` — real and tested, currently only assembled across Wan model-id variants via `create_video_router()`; this task is the first time it chains across different *providers*).
- Produces: `create_providers()`'s `video` field becomes a `VideoRouter` whenever more than one video backend is configured (ModelScope key present, or MiniMax key present, or both); falls back to today's single-backend behavior when only one (or neither) is configured, so existing non-campaign behavior is unchanged.

- [ ] **Step 1: Read `video_router.py`'s `RouterPolicy`/`BackendTier`/`RouteMode` exactly**

Before writing code: `backend/.venv/bin/python -c "import inspect; from app.providers.video_router import RouterPolicy, BackendTier, RouteMode; print(inspect.getsource(RouterPolicy)); print(inspect.getsource(BackendTier)); print(inspect.getsource(RouteMode))"` — confirm the exact constructor fields before Step 2's test assumes them (this plan's prior research confirmed these classes exist and are tested, but not their exact field names).

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_providers_router_assembly.py` (adjust field names to match Step 1's confirmed `RouterPolicy`/`BackendTier` signatures):

```python
"""create_providers() assembles a cross-provider VideoRouter when more than
one video backend is configured; falls back to a single backend otherwise
(today's unchanged behavior)."""

from __future__ import annotations

from app.core.config import Settings
from app.providers import create_providers
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.modelscope import ModelScopeVideoProvider
from app.providers.video import VideoProvider
from app.providers.video_router import VideoRouter


def test_single_backend_when_only_dashscope_configured() -> None:
    providers = create_providers(Settings(dashscope_api_key="test"))
    assert isinstance(providers.video, VideoProvider)


def test_router_assembled_when_modelscope_and_minimax_both_configured() -> None:
    providers = create_providers(
        Settings(
            dashscope_api_key="test",
            modelscope_api_key="ms-key",
            minimax_api_key="mm-key",
        )
    )
    assert isinstance(providers.video, VideoRouter)
    # NOTE (confirmed 2026-07-04 by reading video_router.py): VideoRouter has no
    # public `.backends` attribute — backends are stored privately as `_backends`.
    # `available_backends()` is the public accessor (returns backends whose
    # circuit breaker currently permits a call — equivalent to the raw list here
    # since nothing has failed yet in this test).
    backend_names = {b.name for b in providers.video.available_backends()}
    assert any("modelscope" in n for n in backend_names)
    assert any("minimax" in n for n in backend_names)


def test_router_orders_modelscope_before_minimax() -> None:
    providers = create_providers(
        Settings(
            dashscope_api_key="test",
            modelscope_api_key="ms-key",
            minimax_api_key="mm-key",
        )
    )
    backends = providers.video.available_backends()
    ms_idx = next(i for i, b in enumerate(backends) if "modelscope" in b.name)
    mm_idx = next(i for i, b in enumerate(backends) if "minimax" in b.name)
    assert ms_idx < mm_idx, "free ModelScope must be tried before paid MiniMax"
```

- [ ] **Step 3: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_router_assembly.py -q`
Expected: FAIL (either an import error for `ModelScopeVideoProvider`'s use here, or `isinstance(providers.video, VideoProvider)` failing if the field type/branch doesn't yet exist as assumed — confirm the actual failure matches "router not assembled yet", not an unrelated error).

- [ ] **Step 4: Modify `create_providers()`**

In `backend/app/providers/__init__.py`, widen the `video` variable's type and replace the current `if resolved.video_backend.lower() == "minimax" ... else ...` block (the exact lines returned by this plan's research, ~L124-128) with:

```python
    video: VideoProvider | MiniMaxVideoProvider | ModelScopeVideoProvider | VideoRouter
    video_backends: list[VideoBackend] = []
    if resolved.modelscope_api_key:
        video_backends.append(build_modelscope_video_provider(resolved, usage_sink=client.usage_sink))
    if resolved.minimax_api_key:
        video_backends.append(build_minimax_video_provider(resolved, usage_sink=client.usage_sink))
    if len(video_backends) > 1:
        video = VideoRouter(video_backends, policy=RouterPolicy(mode=RouteMode.COST_AWARE))
    elif len(video_backends) == 1:
        video = video_backends[0]
    else:
        video = VideoProvider(client)
```

(Add a `build_modelscope_video_provider(settings, *, usage_sink) -> ModelScopeVideoProvider` factory function to `modelscope.py`, mirroring however `build_minimax_video_provider` is already structured — check its exact signature in `minimax.py`/`__init__.py` first so the two factories match shape.) Add the necessary imports (`ModelScopeVideoProvider`, `build_modelscope_video_provider`, `VideoRouter`, `RouterPolicy`, `RouteMode`, `VideoBackend`) at the top of `__init__.py`.

- [ ] **Step 5: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_router_assembly.py -q`
Expected: PASS.

- [ ] **Step 6: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_providers_router_assembly.py tests/test_providers_minimax.py tests/test_providers_modelscope.py tests/test_providers_video.py -q` → confirm PASS (nothing regresses).
Run: `make lint` → confirm PASS.

- [ ] **Step 7: Set the campaign's actual budget ceiling**

In `backend/.env` (already gitignored, not committed), confirm/set `BUDGET_CEILING_USD=15.0` (tightening the $30 code default per this plan's Global Constraints) and `MODELSCOPE_API_KEY=` (left blank until the user supplies one — the router assembly already degrades to MiniMax-only when it's absent, per Step 4's `len(video_backends)` branching).

- [ ] **Step 8: Commit**

```bash
git add backend/app/providers/__init__.py backend/app/providers/modelscope.py backend/tests/test_providers_router_assembly.py
git commit -m "feat(backend): route video generation across ModelScope + MiniMax by cost tier"
```

---

### Task 4: Whole-book long-range continuity audit

**Files:**
- Create: `backend/app/render/book_continuity_audit.py`
- Test: `backend/tests/test_book_continuity_audit.py`

**Interfaces:**
- Consumes: `Shot` DB rows in reading order for one book (the same shape `review_export.py` already queries: `source_span`, `beat_id`, `scene_id`, `status`, `qa`, `render_mode`), plus canon `active_states` (`app.memory.interfaces.CanonSlice`, already used by `event_director.py`'s `_wardrobe_from_canon`/`_time_of_day_from`/`_lighting_from`/`_setting_from` helpers — reuse those exact functions rather than re-deriving the same logic).
- Produces:
  - `class LongRangeDrift(NamedTuple/dataclass)`: `from_shot_id: str`, `to_shot_id: str`, `dimension: str`, `from_value: str`, `to_value: str`, `confidence: Literal["high", "low"]`.
  - `class BookContinuityReport(dataclass)`: `book_id: str`, `drifts: tuple[LongRangeDrift, ...]`, `ok: bool` (property: `not drifts`).
  - `def audit_book_continuity(book_id: str, shots_in_reading_order: Sequence[ShotLike], canon_snapshots_by_shot: Mapping[str, CanonSlice]) -> BookContinuityReport` (pure — geometry/canon lookups are the caller's job, mirroring `continuity_qa.score_event_continuity`'s "orchestrator probes, this module scores" split).

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the whole-book long-range continuity audit. Pure, no DB/ffmpeg."""

from __future__ import annotations

from app.render.book_continuity_audit import (
    BookContinuityReport,
    audit_book_continuity,
)


class _FakeShot:
    def __init__(self, shot_id, beat_index, wardrobe=None, hand_off="", summary=""):
        self.shot_id = shot_id
        self.beat_index = beat_index
        self.wardrobe = wardrobe
        self.hand_off = hand_off
        self.summary = summary


def test_no_drift_when_wardrobe_never_changes() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot("s2", 5, wardrobe="blue coat"),
        _FakeShot("s3", 40, wardrobe="blue coat"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok
    assert report.drifts == ()


def test_unmotivated_wardrobe_change_flagged_high_confidence() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot("s2", 40, wardrobe="red coat", hand_off="", summary="she walks on"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert len(report.drifts) == 1
    drift = report.drifts[0]
    assert drift.dimension == "wardrobe"
    assert drift.from_value == "blue coat"
    assert drift.to_value == "red coat"


def test_motivated_wardrobe_change_not_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2", 40, wardrobe="red coat",
            hand_off="she changes into her red coat before the ball",
            summary="",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok


def test_far_apart_change_after_fresh_establishing_shot_not_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot("s2", 200, wardrobe="travelling cloak", summary="A new chapter opens, weeks later, in a different city."),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok  # a fresh establishing shot legitimately resets context
```

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_book_continuity_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.render.book_continuity_audit'`.

- [ ] **Step 3: Write the minimal implementation**

```python
"""Whole-book long-range continuity audit (the seventh crew role, kinora.md's
"the story is accurate" made concrete at book scale).

app.render.continuity_qa scores drift between ADJACENT shots inside one event.
This module walks an ENTIRE book's accepted shots in reading order and flags
persistence-dimension changes (wardrobe/setting/lighting/time_of_day) that are
neither motivated by the shot's own text nor preceded by a fresh establishing
shot far enough away to plausibly be the story moving on rather than an error.
Pure: no ffmpeg, no DB — the caller supplies already-loaded shot data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence

#: A far-apart change is presumed to be legitimate story development (not
#: drift) once this many beats have passed without comment — a full chapter
#: easily exceeds this, a same-scene flicker does not.
_FRESH_ESTABLISHING_GAP_BEATS = 15

_PERSISTENCE_DIMENSIONS: tuple[str, ...] = ("wardrobe", "setting", "lighting", "time_of_day")


class ShotLike(Protocol):
    """The minimal shape this module needs from a book's shots, in reading order."""

    shot_id: str
    beat_index: int
    wardrobe: str | None
    hand_off: str
    summary: str


@dataclass(frozen=True, slots=True)
class LongRangeDrift:
    from_shot_id: str
    to_shot_id: str
    dimension: str
    from_value: str
    to_value: str
    confidence: Literal["high", "low"]

    def describe(self) -> str:
        return (
            f"{self.dimension} drifted from {self.from_value!r} to {self.to_value!r} "
            f"between {self.from_shot_id} and {self.to_shot_id} "
            f"({self.confidence}-confidence, no motivated change found)"
        )


@dataclass(frozen=True, slots=True)
class BookContinuityReport:
    book_id: str
    drifts: tuple[LongRangeDrift, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.drifts


def _motivated(shot: ShotLike, new_value: str) -> bool:
    text = f"{shot.hand_off} {shot.summary}".lower()
    return new_value.lower() in text


def audit_book_continuity(
    book_id: str,
    shots_in_reading_order: Sequence[ShotLike],
    *,
    canon_snapshots_by_shot: dict[str, object] | None = None,
) -> BookContinuityReport:
    """Walk every shot in reading order; flag unmotivated long-range drift.

    ``canon_snapshots_by_shot`` (canon state at each shot's point in the story,
    §8.3) is accepted for the caller's future use (cross-checking a shot
    against a canon fact locked after its original render) but not yet
    required by the wardrobe/setting/lighting/time_of_day checks below, which
    operate on the shots' own directives.
    """
    drifts: list[LongRangeDrift] = []
    last_value: dict[str, tuple[str, ShotLike]] = {}
    for shot in shots_in_reading_order:
        value = getattr(shot, "wardrobe", None)
        if value is None:
            continue
        prior = last_value.get("wardrobe")
        if prior is not None:
            prior_value, prior_shot = prior
            if value != prior_value:
                gap = shot.beat_index - prior_shot.beat_index
                if _motivated(shot, value):
                    pass  # a named, motivated change — not drift
                elif gap >= _FRESH_ESTABLISHING_GAP_BEATS and shot.summary:
                    pass  # far enough + a fresh establishing shot — story moved on
                else:
                    drifts.append(
                        LongRangeDrift(
                            from_shot_id=prior_shot.shot_id,
                            to_shot_id=shot.shot_id,
                            dimension="wardrobe",
                            from_value=prior_value,
                            to_value=value,
                            confidence="high" if gap < _FRESH_ESTABLISHING_GAP_BEATS else "low",
                        )
                    )
        last_value["wardrobe"] = (value, shot)
    return BookContinuityReport(book_id=book_id, drifts=tuple(drifts))


__all__ = ["BookContinuityReport", "LongRangeDrift", "ShotLike", "audit_book_continuity"]
```

- [ ] **Step 4: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_book_continuity_audit.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Extend to the remaining three dimensions**

Repeat Step 1-4's pattern for `setting`, `lighting`, `time_of_day` (currently only `wardrobe` is wired above): generalize the single `"wardrobe"`-shaped block in `audit_book_continuity` into a loop over `_PERSISTENCE_DIMENSIONS`, with one `test_unmotivated_<dimension>_change_flagged` test per remaining dimension added to the test file first (red), then the loop generalization (green). Follow the same red-green cycle as Steps 1-4, not a shortcut.

- [ ] **Step 6: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_book_continuity_audit.py -q` → confirm PASS, all 4 dimensions covered.
Run: `make lint` → confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/render/book_continuity_audit.py backend/tests/test_book_continuity_audit.py
git commit -m "feat(backend): add whole-book long-range continuity audit"
```

---

### Task 5: Generalize `seed_public_domain.py` to the 10 campaign books

**Files:**
- Modify: `backend/scripts/seed_public_domain.py` (the `BOOKS` list, ~L27-32)

**Interfaces:**
- Consumes: nothing new — the rest of the file (download/upload/poll/report) is already title-agnostic (confirmed by this plan's research; the only book-specific surface is the `BOOKS` tuple list).
- Produces: the same live-API ingest flow, now driving 10 books instead of 5.

- [ ] **Step 1: Replace the `BOOKS` list**

In `backend/scripts/seed_public_domain.py`, replace the existing 5-tuple `BOOKS` list with (Gutenberg IDs confirmed directly against `assets/books/catalog.json` on 2026-07-04 — do not re-derive from memory):

```python
BOOKS = [
    (11, "Alice's Adventures in Wonderland", "Lewis Carroll",
     "whimsical storybook, dreamlike"),
    (1342, "Pride and Prejudice", "Jane Austen",
     "warm regency drawing-room, soft daylight"),
    (2701, "Moby Dick; Or, The Whale", "Herman Melville",
     "nautical, stormy grays and whale-bone white"),
    (84, "Frankenstein; or, the modern prometheus", "Mary Wollstonecraft Shelley",
     "gothic, moody candlelight and alpine ice"),
    (2554, "Crime and Punishment", "Fyodor Dostoyevsky",
     "grim St. Petersburg, desaturated ochre"),
    (1184, "The Count of Monte Cristo", "Alexandre Dumas",
     "lavish period adventure, gold and shadow"),
    (345, "Dracula", "Bram Stoker",
     "victorian gothic horror, candlelit fog"),
    (2852, "The Hound of the Baskervilles", "Arthur Conan Doyle",
     "atmospheric moorland mystery, cold mist"),
    (55, "The Wonderful Wizard of Oz", "L. Frank Baum",
     "vivid storybook fantasy, color-coded regions"),
    (120, "Treasure Island", "Robert Louis Stevenson",
     "swashbuckling adventure, sun-bleached sails"),
]
```

- [ ] **Step 2: Dry-run against the live stack (not a unit test — this script drives the real API)**

Run (stack already up per this campaign's setup): `backend/.venv/bin/python backend/scripts/seed_public_domain.py`
Expected: the script logs a download + upload + poll cycle for all 10 titles in sequence and prints a `=== SEED SUMMARY ===` table at the end with `status=ready` for each (per the script's own existing summary logic) — this WILL take a long time (real ingest, real Qwen-VL page analysis per book) and is genuinely Part C's work to run for real; this task's job is only to confirm the script itself is correct, so it is acceptable to Ctrl-C after the first 1-2 books download+start-ingesting successfully and confirm no immediate error, rather than waiting for the full multi-hour run here.

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/seed_public_domain.py
git commit -m "chore: generalize seed_public_domain.py to the 10 QA-campaign books"
```

---

## Part B — Live event-granularity wiring (highest risk; proven before any book runs against it)

### Task 6: `LiveEventShotRenderer` — the adapter between `VideoBackend` and `EventShotRenderer`, WITH the per-shot Critic gate

**Files:**
- Create: `backend/app/render/live_event_renderer.py`
- Modify: `backend/app/render/event_director.py` (add `degraded: bool = False` to the existing `RenderedShot` dataclass — see Step 3)
- Test: `backend/tests/test_live_event_renderer.py`

**Interfaces:**
- Consumes: **`Generator.render(...) -> GeneratorOutput`** (`backend/app/agents/generator.py:195-203`), NOT a raw `VideoBackend` — see the correction below, this changed from an earlier plan draft; `Critic.score(...) -> QARecord` (`backend/app/agents/critic.py` / `backend/app/agents/contracts.py` — exact confirmed signature and types in Step 0 below).
- Produces: `class LiveEventShotRenderer` satisfying `EventShotRenderer` (`event_director.py`: `async def render_shot(self, shot: EventShot, *, still: bytes | None, audio: bytes | None) -> RenderedShot`) — this is what makes event-granularity NOT regress the single-shot accuracy gate: every shot inside an event still gets scored by the same Critic before being accepted.

**Correction, confirmed 2026-07-04 (a real gap caught before dispatch, not during it): wrap `Generator`, not a raw `VideoBackend`.** `WanSpec.image_url` (`backend/app/providers/types.py:205`) is a URL/data-URI **string** — there is no bytes field on `WanSpec`. An earlier draft of this task tried to build a `WanSpec` directly from `still: bytes`, which cannot work as written (nothing in that draft ever turns bytes into a URL). The existing shot-granularity path already solves exactly this problem via `Generator.render(spec: ShotSpec, *, narration_text: str, voice_id: str, reference_image_bytes: list[bytes] | None = None, prev_last_frame_bytes: bytes | None = None) -> GeneratorOutput` (`generator.py:195-203`), which internally calls `build_wan_spec(spec, reference_image_bytes=..., prev_last_frame_bytes=...)` (`generator.py:214`, also exported in `__all__`) to do the actual bytes→`WanSpec` translation, then `self._video.render(wan_spec)` (where `self._video` is exactly the `VideoBackend`/`VideoRouter` Task 3 built — `Generator.__init__(providers, *, video_backend: VideoBackend | None = None)`, `generator.py:189`), then best-effort TTS narration. Wrapping `Generator` instead of a raw `VideoBackend` means this task inherits the byte-to-URL handling for free instead of reinventing it (badly). `ShotSpec` (`backend/app/agents/contracts.py:310-322`, confirmed verbatim): `shot_id: str, beat_id: str | None, scene_id: str | None, render_mode: RenderMode, prompt: str, negative_prompt: str | None, reference_image_ids: list[str], camera: Camera, seed: int, target_duration_s: float, end_frame_ref: str | None`. `GeneratorOutput` (`generator.py:109-121`): `clip_bytes: bytes | None, clip_url: str | None, last_frame_bytes: bytes | None, duration_s: float, audio_bytes: bytes, sample_rate: int, word_timestamps: list[TtsWord], provider_task_id: str | None`.

**Read the exact live call site before writing this task's code**: `backend/app/render/pipeline.py` lines 664-687 (inside `_render_shot`) show precisely how the shot-granularity path sources every argument this task also needs: `ctx.narration_text`/`ctx.voice_id` (from a per-shot render context built earlier in the same method — read enough of `_render_shot`, starting around line 550, to see how `ctx` itself is assembled), `ref_bytes` (locked character reference bytes), `prev_frame` (the previous accepted shot's last frame), and — critically — `frames = await self._frames(output)` immediately after the `Generator.render` call, THEN `self._critic.score(shot_id=ctx.shot_id, clip_frames=frames, canon_slice=ctx.canon_slice, character_crop=frames[0] if frames else None, locked_ref_image=locked_ref, scene_style_centroid=style_centroid, ...)`. **`clip_frames` is NOT `[output.clip_bytes]`** — it's whatever `self._frames(output)` produces (find and read that helper; it almost certainly decodes/samples actual frame images from the clip, not the raw video bytes). Mirror this exact pattern for the `EventShot`-based equivalent: build a `ShotSpec` from the `EventShot` (`shot_id`, `beat_id`, `render_mode`, `prompt=shot.summary`, `camera=shot.camera`, `target_duration_s=shot.duration_s`; `scene_id` comes from the parent `EventScript`, not the shot — thread it in as a constructor field or an extra `render_shot` argument since `EventShotRenderer`'s protocol doesn't carry it), call `self._generator.render(spec, narration_text=..., voice_id=..., reference_image_bytes=[still] if still else None, prev_last_frame_bytes=...)`, extract frames the same way `_frames` does, then score with the Critic. `reference_image_ids` (canon-derived locked refs) and `end_frame_ref` (for `first_last_frame` mode) are real open design points without a single obviously-correct source from `EventShot` alone — resolve them by reading how `_render_shot`'s `ctx`/`cur_spec` populates the equivalent `ShotSpec` fields from canon data, and adapt; if genuinely ambiguous after reading, this is a legitimate point to report `NEEDS_CONTEXT` rather than guess.

- [ ] **Step 0: The Critic's real contract (confirmed 2026-07-04 — use exactly this, do not re-derive)**

`Critic` (`backend/app/agents/critic.py:145`, a `BaseAgent`) exposes:

```python
async def score(
    self, *, shot_id: str, clip_frames: list[bytes], canon_slice: CanonSlice,
    character_crop: bytes | None = None, locked_ref_image: bytes | None = None,
    scene_style_centroid: list[float] | None = None,
    textual_evolution_supported: bool = False, retries_exhausted: bool = False,
    character_crops: list[CharacterCrops] | None = None,
) -> QARecord: ...
```

`Verdict`, `RepairAction`, `QARecord` live in `backend/app/agents/contracts.py` (confirmed verbatim, lines 445-497):

```python
class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"

class RepairAction(StrEnum):
    ACCEPT = "accept"
    REGEN_TIGHTEN_REFS = "regen_tighten_refs"
    REPROMPT_STYLE = "reprompt_style"
    REGEN_NEW_SEED = "regen_new_seed"
    RAISE_CONFLICT = "raise_conflict"
    EVOLVE_CANON = "evolve_canon"
    DEGRADE = "degrade"

class QARecord(BaseModel):
    shot_id: str
    ccs: float
    style_drift: float
    timeline_ok: bool
    contradicting_state_id: str | None = None
    motion_artifact: float
    score: float
    verdict: Verdict
    reason: str = ""
    repair_action: RepairAction = RepairAction.ACCEPT
    learned_reward: float | None = None
    flagged_for_review: bool = False
    anomaly_score: float | None = None
    per_character_ccs: dict[str, float] | None = None
    temporal: float | None = None
    aesthetic: float | None = None
```

(An earlier draft of this plan invented a nonexistent `CriticVerdict` class and a bare `"pass"`/`"fail"` string check — that was wrong; use `Verdict`/`RepairAction`/`QARecord` above, which are the real, verified types.)

**`LiveEventShotRenderer`'s policy for routing `repair_action` (this adapter's own scope decision, not a Critic behavior):** `ACCEPT` → ship the clip. `REGEN_TIGHTEN_REFS` / `REPROMPT_STYLE` / `REGEN_NEW_SEED` → all three are "try rendering again" outcomes with different tightening strategies pipeline.py's single-shot path already implements distinctly; this adapter treats all three uniformly as "retry" (loop, up to `max_retries`) rather than replicating each distinct strategy — a disclosed simplification, not a gap, since replicating them exactly is pipeline.py's job and out of scope for this event-level adapter's first version. `RAISE_CONFLICT` / `EVOLVE_CANON` → these need Continuity/Showrunner arbitration or canon editing, which a per-shot renderer adapter cannot resolve alone; treat as an immediate degrade to Ken-Burns for this shot AND log it via `DefectRepo.log(book_id=..., kind="event_shot_needs_arbitration", shot_id=shot.shot_id, detail={"repair_action": qa.repair_action.value})` (see Task 7/11's `DefectRepo.log` pattern below) so it surfaces in the campaign's defect log rather than vanishing silently. `DEGRADE` → fall back to Ken-Burns directly.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for LiveEventShotRenderer: it must (a) call Generator.render for
each event-shot (which itself handles the still-bytes→WanSpec translation via
build_wan_spec — this adapter does not reimplement that), and (b) run the SAME
per-shot Critic gate RenderPipeline._render_shot already runs, degrading to
Ken-Burns on repeated failure exactly like the live single-shot path does — so
switching to event granularity cannot silently drop per-shot accuracy checking."""

from __future__ import annotations

from app.render.event_director import ContinuityDirective, EventShot
from app.render.live_event_renderer import LiveEventShotRenderer
from app.agents.contracts import Camera, QARecord, RenderMode, RepairAction, SourceSpan, Verdict
from app.agents.generator import GeneratorOutput


def _shot(shot_id: str = "s1") -> EventShot:
    return EventShot(
        shot_id=shot_id, ordinal=0, render_mode=RenderMode.VIDEO_CONTINUATION,
        summary="a quiet meadow", camera=Camera(), duration_s=5.0,
        source_span=SourceSpan(), directive=ContinuityDirective(),
    )


class _FakeGenerator:
    """Fakes Generator.render's exact signature (generator.py:195-203) — the
    real class already handles still-bytes→WanSpec translation internally, so
    this fake never needs to construct a WanSpec at all."""

    def __init__(self, clip_bytes: bytes = b"CLIP") -> None:
        self._clip_bytes = clip_bytes
        self.calls = 0

    async def render(self, spec, *, narration_text, voice_id, reference_image_bytes=None, prev_last_frame_bytes=None):
        self.calls += 1
        return GeneratorOutput(
            clip_bytes=self._clip_bytes, clip_url=None, last_frame_bytes=b"FRAME",
            duration_s=5.0, audio_bytes=b"", sample_rate=0, word_timestamps=[],
            provider_task_id="t1",
        )


def _qa(verdict: Verdict, repair_action: RepairAction = RepairAction.ACCEPT) -> QARecord:
    """Build a minimal real QARecord for a fake Critic to return."""
    return QARecord(
        shot_id="s1", ccs=0.95 if verdict == Verdict.PASS else 0.40,
        style_drift=0.02, timeline_ok=True, motion_artifact=0.05,
        score=0.9 if verdict == Verdict.PASS else 0.3,
        verdict=verdict, repair_action=repair_action,
    )


class _FakeCriticAccept:
    async def score(self, **kwargs):
        return _qa(Verdict.PASS)


async def test_renders_via_generator_and_accepts_on_critic_pass() -> None:
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.calls == 1
    assert result.clip_bytes == b"CLIP"
    assert result.shot_id == "s1"


class _FakeCriticRejectThenAccept:
    def __init__(self) -> None:
        self.calls = 0

    async def score(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _qa(Verdict.FAIL, RepairAction.REGEN_NEW_SEED)
        return _qa(Verdict.PASS)


async def test_retries_on_critic_fail_then_accepts() -> None:
    generator = _FakeGenerator()
    critic = _FakeCriticRejectThenAccept()
    renderer = LiveEventShotRenderer(generator=generator, critic=critic, max_retries=2)
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.calls == 2  # rendered again after the first Critic rejection
    assert critic.calls == 2


class _FakeCriticAlwaysReject:
    async def score(self, **kwargs):
        return _qa(Verdict.FAIL, RepairAction.REGEN_NEW_SEED)


async def test_degrades_to_kenburns_after_retry_cap_exhausted() -> None:
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAlwaysReject(), max_retries=2,
    )
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert result.degraded is True  # RenderedShot gains a `degraded: bool = False` field (Step 3)


class _FakeCriticRaisesConflict:
    async def score(self, **kwargs):
        return _qa(Verdict.FAIL, RepairAction.RAISE_CONFLICT)


async def test_conflict_or_canon_evolution_degrades_immediately_without_retry() -> None:
    """RAISE_CONFLICT/EVOLVE_CANON need arbitration this adapter can't do —
    degrade immediately (no retry loop) rather than burn retries pointlessly."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticRaisesConflict(), max_retries=3,
    )
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.calls == 1  # no retries burned on an un-retryable outcome
    assert result.degraded is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_live_event_renderer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.render.live_event_renderer'`.

- [ ] **Step 3: Write the minimal implementation**

```python
"""LiveEventShotRenderer — the adapter that lets the existing Generator agent
(which already owns the still-bytes→WanSpec translation via build_wan_spec,
and the live VideoBackend/VideoRouter call) drive EventDirector's concurrent
multi-shot rendering, WITHOUT dropping the per-shot Critic gate the
shot-granularity live path already runs (RenderPipeline._render_shot). Two
accuracy layers stay intact: this class enforces the per-shot layer;
EventDirector's own _score_continuity enforces the seam layer on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.agents.contracts import RepairAction, ShotSpec, Verdict
from app.render.degrade import ken_burns_over_image
from app.render.event_director import EventShot, RenderedShot
from app.db.repositories.defect import DefectRepo  # confirmed 2026-07-04: backend/app/db/repositories/defect.py:14

#: repair_action values worth one more render attempt with the same shot
#: (this adapter does not replicate each strategy's distinct tightening —
#: that stays pipeline.py's job — it just tries again, up to max_retries).
_RETRYABLE = frozenset({
    RepairAction.REGEN_TIGHTEN_REFS, RepairAction.REPROMPT_STYLE, RepairAction.REGEN_NEW_SEED,
})
#: repair_action values this per-shot adapter cannot resolve alone (need
#: Continuity/Showrunner arbitration or canon editing) — degrade immediately,
#: do not burn retries, but log it so it surfaces in the campaign's defect log.
_NEEDS_ARBITRATION = frozenset({RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON})


@dataclass(slots=True)
class LiveEventShotRenderer:
    generator: object  # app.agents.generator.Generator (already wraps the live VideoBackend/VideoRouter)
    critic: object  # Critic (backend/app/agents/critic.py) — see Task 6 Step 0
    scene_id: str | None = None  # EventScript.scene_id — EventShot itself doesn't carry it; thread it from the caller (Task 9's worker dispatch has the EventScript in scope)
    book_id: str = ""  # needed for DefectRepo.log's required book_id
    defect_repo: object | None = None  # DefectRepo; None is valid for unit tests that don't exercise the arbitration branch
    max_retries: int = 2

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        started = time.monotonic()
        spec = ShotSpec(
            shot_id=shot.shot_id, beat_id=shot.beat_id, scene_id=self.scene_id,
            render_mode=shot.render_mode, prompt=shot.summary, camera=shot.camera,
            target_duration_s=shot.duration_s,
            # reference_image_ids / end_frame_ref: resolve per Task 6's own
            # "Read the exact live call site" note above (canon-derived —
            # not available on EventShot alone); left as ShotSpec defaults
            # (empty list / None) here ONLY if that reading confirms no
            # simpler source exists for the event-granularity path.
        )
        attempt = 0
        while attempt < self.max_retries:
            attempt += 1
            output = await self.generator.render(
                spec, narration_text="", voice_id="",  # source these the same way pipeline.py's ctx.narration_text/ctx.voice_id are built — read that construction before finalizing, do not leave these as empty strings in the real (non-test) implementation
                reference_image_bytes=[still] if still else None,
                prev_last_frame_bytes=None,  # source from the previous shot's accepted last frame the same way pipeline.py's `prev_frame` is sourced, once EventDirector's fan-out order gives you access to it
            )
            frames = [output.clip_bytes] if output.clip_bytes else []  # placeholder — replace with whatever pipeline.py's own `self._frames(output)` helper does (Task 6's "Read the exact live call site" note); do not ship this literal placeholder in the real implementation
            qa = await self.critic.score(
                shot_id=shot.shot_id, clip_frames=frames,
                canon_slice=None,  # thread the real CanonSlice through the same way pipeline.py's ctx.canon_slice is sourced, not None, in the real (non-test) code path
            )
            if qa.verdict == Verdict.PASS:
                finished = time.monotonic()
                return RenderedShot(
                    shot_id=shot.shot_id, ordinal=shot.ordinal,
                    clip_bytes=output.clip_bytes,
                    last_frame_bytes=output.last_frame_bytes,
                    duration_s=output.duration_s, render_mode=shot.render_mode,
                    started_at=started, finished_at=finished,
                )
            if qa.repair_action in _NEEDS_ARBITRATION:
                if self.defect_repo is not None:
                    await self.defect_repo.log(
                        book_id=self.book_id, kind="event_shot_needs_arbitration",
                        shot_id=shot.shot_id, detail={"repair_action": qa.repair_action.value},
                    )
                break  # do not retry an un-retryable outcome; fall through to degrade
            if qa.repair_action not in _RETRYABLE:
                break  # DEGRADE or anything else unexpected — stop retrying
        # Retry cap exhausted, or a non-retryable outcome — degrade to Ken-Burns
        # rather than ship an unverified clip (mirrors RenderPipeline's degrade path).
        finished = time.monotonic()
        clip = ken_burns_over_image(still, shot.duration_s) if still else b""
        return RenderedShot(
            shot_id=shot.shot_id, ordinal=shot.ordinal, clip_bytes=clip,
            last_frame_bytes=still, duration_s=shot.duration_s,
            render_mode=shot.render_mode, started_at=started, finished_at=finished,
            degraded=True,
        )
```

**Honest flag on this sample code's remaining gaps (do not transcribe blindly — this is weaker than the rest of this plan's code samples and says so):** `narration_text`/`voice_id`, `prev_last_frame_bytes`, `frames` extraction, and `canon_slice` are ALL left as placeholders above specifically because they depend on reading `pipeline.py`'s `_render_shot`/`ctx` construction (lines ~550-687) first, per this task's own "Interfaces" section instruction — resolving them without that reading would be exactly the kind of guess this plan has been avoiding throughout. Do that reading as your literal first implementation step, before writing Step 1's tests, so the tests assert the REAL sourcing pattern rather than the placeholders shown here.

**`RenderedShot` needs a new field**: add `degraded: bool = False` to `event_director.py`'s existing `RenderedShot` dataclass (it does not have this field today — confirmed by reading the class in full earlier in this plan's research) as part of this task, since `LiveEventShotRenderer` is this field's first producer. This is a small, additive, backward-compatible dataclass change (existing callers that don't pass `degraded` get the default `False`).

**Import path confirmed 2026-07-04:** `DefectRepo` is defined at `backend/app/db/repositories/defect.py:14` (a `BaseRepository` subclass) — `from app.db.repositories.defect import DefectRepo` is the real import, no further confirmation needed.

- [ ] **Step 4: Run to verify tests pass**

Run: `backend/.venv/bin/pytest tests/test_live_event_renderer.py -q`
Expected: PASS.

- [ ] **Step 5: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_live_event_renderer.py tests/test_render_event_director.py -q` → confirm PASS.
Run: `make lint` → confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/render/live_event_renderer.py backend/tests/test_live_event_renderer.py
git commit -m "feat(backend): adapt the live video backend + Critic gate into EventDirector's renderer protocol"
```

---

### Task 7: Wire `EventDirector`'s repair loop for real (currently only logs)

**Files:**
- Modify: `backend/app/render/event_director.py` (`EventDirector.render_event`, the `continuity = await self._score_continuity(...)` line and everything after it)
- Test: extend the existing `backend/tests/test_render_event_director.py`

**Interfaces:**
- Consumes: `route_event_continuity(seam) -> SeamRepair` (existing, `continuity_qa.py`), `propose_supplemental_shot` (existing), `detect_persistence_drift` (existing).
- Produces: `render_event` now actually re-renders/inserts/degrades based on the continuity report instead of only logging it.

- [ ] **Step 1: Write the failing test**

```python
async def test_render_event_inserts_supplemental_shot_on_hard_cut() -> None:
    """A seam with no hand-off and no chained mode routes to
    INSERT_SUPPLEMENTAL — render_event must actually add and render the
    supplemental shot, not just note the failure."""
    # Build a 2-shot EventScript where shot 2 does NOT continue from shot 1
    # (continues_from_shot_id=None, render_mode not in _CHAINED_MODES) so
    # route_event_continuity returns INSERT_SUPPLEMENTAL deterministically —
    # reuse plan_event_script's own test fixtures/pattern already in the
    # existing test_render_event_director.py for constructing a valid EventScript.
    ...  # concrete fixture construction follows this file's existing test
    # patterns (already in the repo) rather than being re-invented here.
    director = EventDirector(renderer=_CountingKenBurnsRenderer())
    result = await director.render_event(script)
    assert result.shot_count == 3  # original 2 + 1 supplemental inserted
    assert result.continuity is not None and result.continuity.action.value == "insert_supplemental"


async def test_render_event_degrades_when_repair_route_is_degrade() -> None:
    """A seam with BOTH a geometry failure and a chain failure routes to
    DEGRADE — render_event must fall back to Ken-Burns for that event
    rather than shipping the known-bad seam."""
    ...
```

(Fill the `...` fixture construction from `test_render_event_director.py`'s own established patterns, confirmed 2026-07-04: `_bridge_beats()` (top of the file) returns a ready-made 3-beat "chase across the bridge" `Beat` list suitable for `plan_event_script(...)`; `from tests.test_render_support import FakeObjectStore, make_slice, png_bytes, real_mp4, wav_bytes` provides the canon slice (`make_slice()`), a fake object store, and real still/audio/video byte fixtures already used throughout this file's existing tests — reuse these directly rather than inventing new fixture helpers.)

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_render_event_director.py -k repair -q`
Expected: FAIL — `result.shot_count == 3` fails (currently always equals the original shot count; nothing is inserted today).

- [ ] **Step 3: Implement the repair loop**

In `EventDirector.render_event`, after `continuity = await self._score_continuity(script, rendered)`, add branching on `continuity.action`:

```python
        continuity = await self._score_continuity(script, rendered)
        if not continuity.ok:
            script, rendered = await self._repair(script, rendered, continuity, stills=stills, audio=audio)
            # Re-stitch and re-score after repair so the shipped result reflects
            # the corrected event, not the originally-failing one.
            clips = [r.clip_bytes for r in rendered]
            durations = [r.duration_s for r in rendered]
            overlap = effective_crossfade(durations, self._crossfade_s)
            clip_bytes = await anyio.to_thread.run_sync(
                lambda: concat_clips(clips, size=self._film_size, fps=self._fps, crossfade_s=overlap)
            )
            continuity = await self._score_continuity(script, rendered)
```

Add the `_repair` method:

```python
    async def _repair(
        self, script: EventScript, rendered: list[RenderedShot],
        report: EventContinuityReport, *, stills, audio,
    ) -> tuple[EventScript, list[RenderedShot]]:
        """Act on the worst seam's repair action (§9.5-style routing, kinora.md).

        Also logs the repair via the existing DefectRepo.log(book_id=, kind=,
        shot_id=, detail=) pattern (confirmed 2026-07-04, already called live
        from pipeline.py and durability/deadletter.py) — grep `class DefectRepo`
        to confirm its exact import path first. Call it once, right after
        deciding `report.action`, before branching:
        `await self._defect_repo.log(book_id=script.book_id, kind="seam_repair",
        shot_id=failing.to_shot_id, detail={"action": report.action.value})`
        (requires threading a `defect_repo` into `EventDirector.__init__`,
        optional/None-default so existing off-gate/test usage is unaffected).
        """
        from app.render.continuity_qa import SeamRepair, propose_supplemental_shot

        if report.action == SeamRepair.ACCEPT:
            return script, rendered
        # Find the first failing seam to repair (subsequent re-scoring after
        # a repair may resolve or re-flag later seams on the next pass).
        failing = next(s for s in report.seams if not s.ok)
        prev_shot = next(s for s in script.shots if s.shot_id == failing.from_shot_id)
        next_shot = next(s for s in script.shots if s.shot_id == failing.to_shot_id)
        if report.action == SeamRepair.INSERT_SUPPLEMENTAL:
            supplemental = propose_supplemental_shot(
                prev_shot, next_shot, book_id=script.book_id, event_id=script.event_id,
            )
            insert_at = script.shots.index(prev_shot) + 1
            new_shots = list(script.shots)
            new_shots.insert(insert_at, supplemental)
            new_script = script.model_copy(update={"shots": new_shots})
            supplemental_result = await self._renderer.render_shot(
                supplemental, still=(stills or {}).get(prev_shot.shot_id), audio=None,
            )
            new_rendered = list(rendered)
            new_rendered.insert(insert_at, supplemental_result)
            return new_script, new_rendered
        if report.action == SeamRepair.REGEN_CONTINUATION:
            idx = script.shots.index(next_shot)
            re_rendered = await self._renderer.render_shot(
                next_shot, still=(stills or {}).get(prev_shot.shot_id), audio=(audio or {}).get(next_shot.shot_id),
            )
            new_rendered = list(rendered)
            new_rendered[idx] = re_rendered
            return script, new_rendered
        # DEGRADE: fall back to Ken-Burns for every shot in this event rather
        # than ship a known-bad seam.
        from app.render.event_director import KenBurnsEventRenderer

        fallback = KenBurnsEventRenderer(film_size=self._film_size, fps=self._fps)
        new_rendered = [
            await fallback.render_shot(s, still=(stills or {}).get(s.shot_id), audio=(audio or {}).get(s.shot_id))
            for s in script.shots
        ]
        return script, new_rendered
```

- [ ] **Step 4: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_render_event_director.py -q`
Expected: PASS (all existing tests plus the two new ones).

- [ ] **Step 5: Wire `detect_persistence_drift` as a second gate**

Add a test asserting that an unmotivated persistence drift (no geometry problem, but `detect_persistence_drift` finds a flagged dimension) ALSO routes through `_repair` — extend `_score_continuity` (or add a sibling check called right after it in `render_event`) to also run `detect_persistence_drift(script)` and, if it finds drifts, treat that as at least `SeamRepair.REGEN_CONTINUATION` for the drifting shot (never weaker than what the geometry-only score already decided — take the max severity of the two checks). Follow the same red-green cycle as Steps 1-4.

- [ ] **Step 6: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_render_event_director.py -q` → confirm PASS.
Run: `make lint` → confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/render/event_director.py backend/tests/test_render_event_director.py
git commit -m "feat(backend): make EventDirector's seam-continuity repair loop act, not just log"
```

---

### Task 8: Shot model + `ShotResponse` — carry an in-merged-clip offset

**Files:**
- Modify: `backend/app/db/models/shot.py` (the `Shot` model, confirmed verbatim 2026-07-04 at lines 36-84: fields include `book_id, scene_id, beat_id, source_span, status, render_mode, prompt, negative_prompt, seed, reference_set_hash, reference_image_ids, duration_s, output, narration, qa, cost, embedding, canon_version_at_render, shot_hash, accepted_at` — no `clip_start_s`/`clip_end_s` exist yet)
- Modify: `backend/app/api/schemas.py` (confirmed 2026-07-04: a single flat file, NOT a `schemas/` package — `ShotResponse` is at lines 180-195 with fields `shot_id, beat_id, scene_id, source_span, status, render_mode, duration_s, qa, clip_url, reference_image_ids`)
- Test: extend `backend/tests/` — grep `class Shot\b` and `ShotResponse` usages across `backend/tests/` to find the existing model/schema test file(s) before adding to them (not independently confirmed by this plan's research — a 1-step lookup, not a full re-derivation)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Shot.clip_start_s: float | None`, `Shot.clip_end_s: float | None` (both `None` for a normal single-shot clip, preserving today's behavior exactly); `ShotResponse` exposes the same two fields.

- [ ] **Step 1: Confirm the existing test file(s) covering `Shot` and `ShotResponse`**

Run: `grep -rln "class Shot(\|ShotResponse" backend/tests/`. Read whichever file(s) it finds to match existing test-construction conventions (e.g., does `Shot(...)` in tests go through a factory/fixture rather than a bare constructor call — the plan's Step 2 draft below assumes a bare constructor; adjust to match the real convention if a fixture pattern already exists).

- [ ] **Step 2: Write the failing test**

```python
def test_shot_defaults_clip_offsets_to_none() -> None:
    shot = Shot(book_id="b1", status=ShotStatus.PLANNED)
    assert shot.clip_start_s is None
    assert shot.clip_end_s is None


def test_shot_response_exposes_clip_offsets() -> None:
    shot = Shot(book_id="b1", status=ShotStatus.PLANNED, clip_start_s=5.0, clip_end_s=10.0)
    response = ShotResponse.model_validate(shot, from_attributes=True)
    assert response.clip_start_s == 5.0
    assert response.clip_end_s == 10.0
```

(`ShotResponse.model_validate(..., from_attributes=True)` assumes Pydantic v2 — confirmed consistent with this codebase's style, e.g. `ConfigDict(extra="forbid")` usage in `backend/app/agents/contracts.py`. If Step 1 finds `ShotResponse` is actually constructed via a dedicated `ShotResponse.from_shot(shot)`-style helper elsewhere in the codebase instead of raw `model_validate`, use that real convention instead.)

- [ ] **Step 3: Run to verify it fails**

Run: `backend/.venv/bin/pytest <file from Step 1> -k clip_offset -q`
Expected: FAIL — `TypeError: 'clip_start_s' is an invalid keyword argument for Shot` (or Pydantic's equivalent "extra fields not permitted" for the `ShotResponse` case, given its `ConfigDict(extra="forbid")` convention likely applies here too).

- [ ] **Step 4: Add the fields + an Alembic migration**

Add `clip_start_s: Mapped[float | None]` / `clip_end_s: Mapped[float | None]` (nullable, no default needed beyond `None`) to the `Shot` model in `backend/app/db/models/shot.py`, and the same two optional fields (`clip_start_s: float | None = None`, `clip_end_s: float | None = None`) to `ShotResponse` in `backend/app/api/schemas.py`. Generate the migration: `cd backend && .venv/bin/alembic revision --autogenerate -m "add shot clip_start_s/clip_end_s"`. Read the generated migration file to confirm it ONLY adds the two nullable columns (no unrelated autogenerate noise) before proceeding.

- [ ] **Step 5: Run to verify it passes**

Run: `backend/.venv/bin/alembic upgrade head` (against the dev DB), then `backend/.venv/bin/pytest <confirmed test file> -k clip_offset -q`
Expected: PASS.

- [ ] **Step 6: Verification gate**

Run: `backend/.venv/bin/pytest tests/ -k shot -q` → confirm nothing regresses.
Run: `make lint` → confirm PASS. Run `backend/.venv/bin/alembic heads` → confirm still a single head (no fork).

- [ ] **Step 7: Commit**

```bash
git add backend/app/db/models/ backend/app/api/ backend/alembic/versions/
git commit -m "feat(backend): add Shot.clip_start_s/clip_end_s for merged-event-clip playback"
```

---

### Task 9: Scheduler groups shots into events; Worker dispatches event jobs; the merged clip populates Task 8's offsets

**Files:**
- Modify: `backend/app/scheduler/service.py` (`SchedulerService._fill_committed`, the enqueue call)
- Modify: `backend/app/queue/worker.py` (`RenderWorker._default_run_shot`)
- Test: `backend/tests/test_scheduler_event_granularity.py`, `backend/tests/test_worker_event_dispatch.py`

**Interfaces:**
- Consumes: `pack_segments` (existing, `segment_packer.py`), `plan_segment_script` (existing, `event_director.py`), `EventDirector` + `LiveEventShotRenderer` (Task 6-7), `render_granularity` setting (Task 2).
- Produces: when `render_granularity="event"`, a scene's ready shots are grouped and rendered as one event, with each original shot's DB row updated to point at the merged clip (`clip_key` shared across the group) plus its own `clip_start_s`/`clip_end_s` (Task 8) instead of each shot getting a distinct clip. When `render_granularity="shot"` (default), behavior is byte-for-byte unchanged — this is the regression guard.

- [ ] **Step 1: `QueuedJob`'s exact schema and enqueue mechanism (fully confirmed 2026-07-04 — use exactly this)**

`QueuedJob` (`backend/app/queue/redis_queue.py:250-271`) is a **plain (non-frozen) dataclass**, easy to extend: `id, shot_hash, priority, status, book_id, attempts=0, session_id=None, shot_id=None, beat_id=None, scene_id=None, cancel_token=None, reservation_id=None, reserved_video_s=0.0, target_duration_s=5.0, target_word=0, prompt=None, cancelled=False, provider_task_id=None, error=None`. Add `shot_ids: list[str] | None = None` (used only for event jobs; `shot_id` stays populated with the group's first shot id for backward-compatible dedup-key/logging purposes).

**The real mechanism (fully traced, not a guess):** `RedisQueue.enqueue()` (`redis_queue.py:415-460+`) builds a flat `fields: dict[str, str]` (every value already a string — floats via the local `_num()` helper, optional fields via `_put_optional(fields, key, value)` which only sets the key when the value is non-None/non-empty), then the WHOLE `fields` dict is JSON-serialized ONCE (`json.dumps(fields, separators=(",", ":"))`) and passed as a single argument into an atomic `_ENQUEUE_LUA` script (`redis_queue.py:116`) via `redis.eval(...)` — the Lua script decomposes that JSON into the actual Redis hash fields server-side (atomicity is the reason for the Lua round-trip, not something to preserve differently). `from_hash()` (`redis_queue.py:273-297`) then reads that hash back field-by-field, all as strings, converting each back to its real type.

**Exact edit, both sides (fields stays `dict[str, str]`, so a list needs one extra encode/decode hop):**
- In `enqueue()`, after the existing `_put_optional(fields, "prompt", prompt)` line (~`redis_queue.py:461`), add a new `shot_ids: list[str] | None = None` parameter to `enqueue()`'s own signature, and: `_put_optional(fields, "shot_ids", json.dumps(shot_ids) if shot_ids else None)`.
- In `from_hash()`, add: `shot_ids=json.loads(data["shot_ids"]) if data.get("shot_ids") else None,` alongside the other `_opt(...)`-based fields.
- No change needed to `_ENQUEUE_LUA` itself or to `_put_optional`/`_num` — both already handle an arbitrary string value generically.

- [ ] **Step 2: Write the failing Scheduler test**

```python
"""SchedulerService groups shots into events when render_granularity='event';
enqueues one job per shot (unchanged) when render_granularity='shot'."""

async def test_shot_granularity_unchanged_by_default() -> None:
    # existing fixture setup for a session with N ready shots in one scene
    ...
    scheduler = SchedulerService(..., settings=Settings(dashscope_api_key="test"))
    await scheduler._fill_committed(session)
    assert queue.enqueue_calls == N  # one job per shot, exactly today's behavior


async def test_event_granularity_groups_shots_into_packed_segments() -> None:
    ...
    scheduler = SchedulerService(..., settings=Settings(dashscope_api_key="test", render_granularity="event"))
    await scheduler._fill_committed(session)
    assert queue.enqueue_calls < N  # fewer jobs than shots — they were grouped
    assert all(job.shot_ids for job in queue.enqueued_jobs)  # each job carries a group
```

(Fixture setup for "a session with N ready shots in one scene" reuses whatever `test_scheduler.py`'s existing fixtures already build — read that file first rather than re-deriving session/shot/scene test fixtures from scratch.)

- [ ] **Step 3: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_scheduler_event_granularity.py -q`
Expected: FAIL — grouping doesn't exist yet, `enqueue_calls` will equal `N` in both tests.

- [ ] **Step 4: Implement the Scheduler branch (fully re-grounded 2026-07-04 against the REAL current `_fill_committed` — the version below is more accurate than the plan's original draft; use this, not the original pseudocode)**

**The real current loop** (`backend/app/scheduler/service.py:371-470`, confirmed verbatim): `_fill_committed` runs `while session.committed_seconds_ahead < self._high:`, each iteration fetching exactly ONE shot via `shot = await self._shots.next_uncommitted_shot(session.book_id, cursor)` (`ShotRepo.next_uncommitted_shot(book_id, after_word) -> Shot | None`, `db/repositories/shot.py:136` — an ALREADY-PLANNED `Shot` row from ingest, not a raw `Beat`), advances `cursor = max(cursor + 1, start)`, then runs the SAME readiness gates every shot must pass regardless of granularity: `eta_seconds(...) >= self._commit_horizon` (skip if too far/unstable), `remaining_s < est` (skip if budget can't afford it), `self._reserve(session, shot, est)` (skip if a cap is hit) — only THEN does it call `self._queue.enqueue(shot_hash=..., priority=..., book_id=..., job_id=..., session_id=..., shot_id=shot.id, beat_id=shot.beat_id, scene_id=shot.scene_id, cancel_token=..., reservation_id=reservation.id, reserved_video_s=est, target_duration_s=est, target_word=start)`.

**Key discovery: `plan_segment_script`/`pack_segments` operate on raw `Beat` objects, NOT on already-planned `Shot` rows — but `_fill_committed` only ever sees `Shot` rows.** `BeatRepo.list_by_scene(scene_id) -> list[Beat]` (`db/repositories/beat.py:63`) is the real, existing method that bridges this — given a scene_id, it returns all that scene's beats in one call.

**Recommended split of responsibility (confirm this is sound before implementing, or propose a better one and say why):** keep the SCHEDULER'S job unchanged in spirit — it still decides, shot by shot, using the EXACT SAME readiness gates above (eta/stability/budget/reservation), which shots are ready to promote right now. The ONLY change for `render_granularity="event"`: instead of enqueueing each ready shot as its own job immediately, accumulate consecutive ready shots that share the same `scene_id` (a scene's shots are contiguous in word-order, since scenes are sequential text regions) into a batch — stop accumulating when the next candidate shot belongs to a different `scene_id`, when a readiness gate fails for it, or when the batch reaches `MAX_EVENT_SHOTS` (`event_director.MAX_EVENT_SHOTS`, currently 6) — then enqueue ONE job for the whole batch via `shot_ids=[s.id for s in batch]` (reusing `shot.id`/`shot.beat_id`/`shot.scene_id` of the batch's FIRST shot for the job's scalar `shot_id`/`beat_id`/`scene_id` fields, per Step 1's backward-compatible convention). The WORKER (Step 6+ below), on receiving a job with `shot_ids` set, is what actually calls `BeatRepo.list_by_scene`/`plan_segment_script` to turn the group back into a fresh `EventScript` — the Scheduler itself does not need a new `BeatRepo` dependency or to touch `plan_segment_script`/`pack_segments` at all. This keeps the Scheduler's existing per-shot budget/eta/reservation logic completely intact (each shot in the batch still gets its own `_reserve` call, same as today) while only changing HOW the resulting ready shots get enqueued.

If, after reading the real code, this split doesn't hold up (e.g., some invariant `_fill_committed` relies on breaks when shots are batched before enqueueing), stop and report NEEDS_CONTEXT with specifics rather than forcing it — this is this plan's own best-effort design from re-reading the real code, not a settled architectural decision.

```python
        if self._settings.render_granularity == "event":
            # Accumulate consecutive same-scene ready shots (see the design note
            # above) instead of enqueueing this one shot immediately; adapt the
            # surrounding while-loop to batch across iterations rather than
            # enqueueing inside this single iteration. Sketch of the enqueue
            # call once a batch is finalized (replace group/est/start below with
            # the batch's real values, matching Step 1's field conventions):
            result = await self._queue.enqueue(
                shot_hash=_dedup_key(session.book_id, batch[0]),
                priority=RenderPriority.COMMITTED, book_id=session.book_id,
                job_id=new_id(), session_id=session.session_id,
                shot_id=batch[0].id, shot_ids=[s.id for s in batch],
                beat_id=batch[0].beat_id, scene_id=batch[0].scene_id,
                cancel_token=session.trajectory_token,
                reservation_id=reservation.id, reserved_video_s=est,
                    target_duration_s=est, target_word=start,
                )
            return  # grouped path handled; skip the per-shot loop below
        # --- existing per-shot loop, unchanged ---
```

(Adjust variable names — `ready_beats`, `scene_id`, `est`, `start`, `reservation` — to match the ACTUAL surrounding code in `_fill_committed`, which this plan's research read but did not reproduce in full; use the real local variable names from the method, not invented ones.)

- [ ] **Step 5: Run to verify the Scheduler tests pass**

Run: `backend/.venv/bin/pytest tests/test_scheduler_event_granularity.py -q`
Expected: PASS.

- [ ] **Step 6: Write the failing Worker test**

```python
async def test_worker_dispatches_shot_job_to_render_pipeline_unchanged() -> None:
    job = QueuedJob(shot_id="s1", shot_ids=None, book_id="b1", session_id=None)
    worker = RenderWorker(..., settings=Settings(dashscope_api_key="test"))
    await worker._default_run_shot(job)
    assert pipeline_render_shot_called_with == ("b1", "s1")


async def test_worker_dispatches_event_job_to_event_director() -> None:
    job = QueuedJob(shot_id="s1", shot_ids=["s1", "s2", "s3"], book_id="b1", session_id=None)
    worker = RenderWorker(..., settings=Settings(dashscope_api_key="test", render_granularity="event"))
    await worker._default_run_shot(job)
    assert event_director_render_event_called  # not pipeline.render_shot
```

- [ ] **Step 7: Run to verify it fails, then implement**

Run: `backend/.venv/bin/pytest tests/test_worker_event_dispatch.py -q` → FAIL.

In `_default_run_shot`, branch on `job.shot_ids`:

```python
    async def _default_run_shot(self, job: QueuedJob) -> RenderResult:
        if job.shot_ids:
            from app.render.event_director import EventDirector, plan_segment_script
            from app.render.live_event_renderer import LiveEventShotRenderer

            # Confirmed 2026-07-04: BeatRepo.list_by_scene(scene_id) -> list[Beat]
            # (backend/app/db/repositories/beat.py:63) is the real method — the
            # job carries job.scene_id (Step 4's batch used its first shot's
            # scene_id for this field), so look up the BeatRepo the same way
            # build_render_pipeline wires other repos, then filter/order to just
            # this job's shot_ids' corresponding beats if list_by_scene returns
            # more than the batch (a scene may have more beats than fit in one
            # event batch, per Step 4's MAX_EVENT_SHOTS cap).
            beat_repo = BeatRepo(db)  # confirm exact constructor/DI pattern against how other repos are built in this file
            beats = await beat_repo.list_by_scene(job.scene_id)
            script = plan_segment_script(
                event_id=job.job_id, book_id=job.book_id, scene_id=job.scene_id, beats=beats,
            )
            # Task 6 corrected LiveEventShotRenderer to wrap a Generator, not a
            # raw VideoBackend — build one the same way build_render_pipeline
            # does (Generator(providers, video_backend=providers.video)), so it
            # carries Task 3's router and gets the byte->WanSpec translation
            # (build_wan_spec) for free rather than reinventing it here.
            from app.agents.generator import Generator

            generator = Generator(self._providers, video_backend=self._providers.video)
            renderer = LiveEventShotRenderer(
                generator=generator, critic=self._critic, scene_id=job.scene_id, book_id=job.book_id,
            )
            director = EventDirector(renderer=renderer, store=self._object_store)
            result = await director.render_event(script)
            return self._to_render_result(result)  # adapt EventRenderResult -> this method's existing RenderResult return type
        from app.render.pipeline import build_render_pipeline
        db = ...
        pipeline = build_render_pipeline(db, providers=self._providers, object_store=self._object_store, settings=self._settings)
        return await pipeline.render_shot(job.book_id, job.shot_id, session_id=job.session_id, director_present=job.session_id is not None)
```

- [ ] **Step 8: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_worker_event_dispatch.py -q`
Expected: PASS.

- [ ] **Step 9: Persist Task 8's offsets from the merged event result**

Extend `_to_render_result` (or wherever `EventRenderResult` gets turned into DB updates) so that for each of the group's original shot rows, `clip_key` is set to the event's shared `clip_key`, and `clip_start_s`/`clip_end_s` are set from the merged `SceneSyncMap`'s per-shot `video_start_s`/`video_end_s` (already produced by `merge_sync_segments`, confirmed by this plan's research). Add a test asserting all N original shots in the group end up with the SAME `clip_key` and DIFFERENT, correctly-ordered `clip_start_s` values.

- [ ] **Step 9b: Wire the read path too — `_shot_response()` doesn't expose Task 8's fields yet (confirmed gap, found during Task 8's review)**

Task 8 added `clip_start_s`/`clip_end_s` to `ShotResponse`, but the ONLY production site that constructs a `ShotResponse` — `_shot_response()` in `backend/app/api/routes/books.py:522-539` — builds it via explicit keyword arguments (confirmed: it does NOT use `model_validate`/`from_attributes`, so a field existing on the Pydantic model does nothing by itself). Without this step, every real API response would send `clip_start_s: null, clip_end_s: null` regardless of what Step 9 just wrote to the database — Task 10's client code would pass its own unit tests (which build `ShotResponse`-shaped fixtures directly) while silently never receiving real values from the live app. Add `clip_start_s=shot.clip_start_s, clip_end_s=shot.clip_end_s` to `_shot_response()`'s existing kwargs (`books.py:528-539`). Add a test asserting `_shot_response(shot_with_offsets)` returns a `ShotResponse` with the real, non-None values — this is the test that actually proves the end-to-end wiring works, not just that the DB column exists.

- [ ] **Step 10: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_scheduler_event_granularity.py tests/test_worker_event_dispatch.py tests/test_scheduler.py tests/test_worker.py -q` → confirm PASS, existing shot-granularity tests unchanged.
Run: `make lint` → confirm PASS.

- [ ] **Step 11: Commit**

```bash
git add backend/app/scheduler/service.py backend/app/queue/worker.py backend/tests/test_scheduler_event_granularity.py backend/tests/test_worker_event_dispatch.py
git commit -m "feat(backend): live-wire event-granularity rendering behind render_granularity=event"
```

---

### Task 10: Client — exercise the already-built merged-clip seek path

**Files:**
- Modify: `apps/desktop/src/reading/ScrollFilmEngine.tsx` (`timelineFromProps`, ~L68-111)
- Test: extend `apps/desktop/src/reading/__tests__/timeline.test.ts` (confirmed to already exist 2026-07-04 — do not create a duplicate file)

**Interfaces:**
- Consumes: `ShotResponse.clip_start_s`/`clip_end_s` (Task 8), the EXISTING `FilmSegment`/`buildTimeline` (`timeline.ts` — already correct, per this plan's research; not modified by this task).
- Produces: when a group of shots shares a `clip_key` (i.e., their API `clip_url`s are identical), `timelineFromProps` emits `SegmentInput`s with that SHARED `src` and each shot's real `clipStart`/`clipEnd` (from `clip_start_s`/`clip_end_s`) instead of defaulting each to its own whole-clip `[0, duration]`.

- [ ] **Step 1: Confirm the exact current `timelineFromProps` code and `SegmentInput` type**

Read `apps/desktop/src/reading/ScrollFilmEngine.tsx` lines 68-111 and `timeline.ts`'s `SegmentInput`/`FilmSegment` types in full (already summarized by this plan's research; re-read directly before editing since exact current code, not a summary, is what a diff applies to).

- [ ] **Step 2: Write the failing test**

```typescript
// apps/desktop/src/reading/timeline.test.ts (or wherever existing timeline.ts tests live — check first)
import { describe, expect, it } from "vitest";
import { timelineFromProps } from "./ScrollFilmEngine"; // adjust export if not currently exported — export it if needed for testability

describe("timelineFromProps merged-clip grouping", () => {
  it("gives each shot its own src when clip_start_s/clip_end_s are absent (today's unchanged behavior)", () => {
    const shots = [
      { id: "s1", clip_url: "http://x/s1.mp4", duration_s: 5, clip_start_s: null, clip_end_s: null, source_span: { word_range: [0, 10] } },
      { id: "s2", clip_url: "http://x/s2.mp4", duration_s: 5, clip_start_s: null, clip_end_s: null, source_span: { word_range: [10, 20] } },
    ];
    const segments = timelineFromProps(shots as any);
    expect(segments[0].src).not.toBe(segments[1].src);
  });

  it("groups shots sharing a clip_key into one src with real clipStart/clipEnd offsets", () => {
    const shots = [
      { id: "s1", clip_url: "http://x/event1.mp4", duration_s: 15, clip_start_s: 0, clip_end_s: 5, source_span: { word_range: [0, 10] } },
      { id: "s2", clip_url: "http://x/event1.mp4", duration_s: 15, clip_start_s: 5, clip_end_s: 10, source_span: { word_range: [10, 20] } },
      { id: "s3", clip_url: "http://x/event1.mp4", duration_s: 15, clip_start_s: 10, clip_end_s: 15, source_span: { word_range: [20, 30] } },
    ];
    const segments = timelineFromProps(shots as any);
    expect(new Set(segments.map((s) => s.src)).size).toBe(1);
    expect(segments[0].clipStart).toBe(0);
    expect(segments[1].clipStart).toBe(5);
    expect(segments[2].clipStart).toBe(10);
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `pnpm --filter @kinora/desktop run test -- timeline.test.ts`
Expected: FAIL on the second case (today's code always builds a distinct `src` per shot regardless of a shared `clip_url`).

- [ ] **Step 4: Implement**

In `timelineFromProps`, change each shot's `SegmentInput` construction from unconditionally using its own `clip_url` + defaulting `clipStart`/`clipEnd`, to: `src = shot.clip_url` (unchanged — it's already naturally shared when the backend gave two shots the same `clip_key`/`clip_url`, per Task 9); `clipStart = shot.clip_start_s ?? 0`; `clipEnd = shot.clip_end_s ?? shot.duration_s`. This is the entire change — `buildTimeline`/`resolvePlayhead`/`segmentTime` already handle a shared `src` correctly per this plan's research, so no changes there.

- [ ] **Step 5: Run to verify it passes**

Run: `pnpm --filter @kinora/desktop run test -- timeline.test.ts`
Expected: PASS.

- [ ] **Step 6: Real end-to-end verification (not a unit test — a one-time Playwright pass, per this repo's established pattern for verifying the actual app)**

Following the pattern already used for this project (`coordination/artifacts/agent-12/`'s walkthrough): drive the built renderer at `:5173` with the project's bundled Playwright/chromium, open a seeded book with `render_granularity=event` and `KINORA_LIVE_VIDEO` on, scroll across a word range spanning 2+ shots known to share one merged clip, and confirm the video element's `currentTime` seeks correctly within the single shared `<video src>` (no reload, no black flash) rather than switching `src`. Capture this as a screenshot/short recording — this becomes part of Part C's Task 8 edge-case-6 verification artifact (see spec Section 8, item covering the merged-clip seek), not a new standalone artifact.

- [ ] **Step 7: Verification gate**

Run: `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test && pnpm --filter @kinora/desktop run build` → confirm all green.

- [ ] **Step 8: Commit**

```bash
git add apps/desktop/src/reading/ScrollFilmEngine.tsx apps/desktop/src/reading/timeline.test.ts
git commit -m "feat(desktop): exercise the merged-clip seek path when shots share a clip_key"
```

---

### Task 11: Extend `review_export.py` — numeric scores, repair actions, long-range findings, cross-book index

**Files:**
- Modify: `backend/app/cli/actions/review_export.py`
- Test: extend `backend/tests/test_cli_integration.py`

**Interfaces:**
- Consumes: `shot.qa` (existing field, already includes `ccs`/`style_drift` per the fake-seed engine's shape found in Task 5's research — confirm the REAL live Critic's `qa` dict shape matches, since that fake-seed shape is exactly what NOT to trust blindly), `BookContinuityReport` (Task 4), the seam-repair action recorded per event (Task 7 — confirm exactly where/how `EventContinuityReport`/repair actions get persisted onto shot rows so this task reads real persisted data, not in-memory-only results that vanish after the render call returns).
- Produces: `manifest.json` entries gain `qa_ccs: float | None`, `qa_style_drift: float | None`, `seam_repair_action: str | None`; a new top-level `long_range_findings: list[dict]` in the manifest; `index.html` renders numeric scores instead of only a pass/fail badge; a new `qa_campaign_report.py` script (Task structure table) builds the cross-book index.

- [ ] **Step 1: Persist repair actions via the existing `DefectRepo.log`, confirmed 2026-07-04**

`Defect` (`backend/app/db/models/defect.py:19-31`): `shot_id (nullable), book_id, kind: str, detail: dict | None`. It is never constructed directly — the real, established pattern is `DefectRepo.log(*, book_id, kind, shot_id=None, detail=None, defect_id=None) -> Defect`, already called live from `backend/app/render/pipeline.py:1314` and `backend/app/render/durability/deadletter.py:134`. Task 7's `_repair` method should call this SAME `DefectRepo.log(book_id=script.book_id, kind="seam_repair", shot_id=<the repaired shot's id>, detail={"action": report.action.value})` when it fires a repair (add this call to Task 7's `_repair` implementation if not already present — cross-reference Task 7 above and add it there, not as new code in this task), so this task's export logic has real, already-persisted data to read rather than inventing a second persistence path. `DefectRepo` is confirmed at `backend/app/db/repositories/defect.py:14` — `from app.db.repositories.defect import DefectRepo`.

- [ ] **Step 1b: Confirm — and if needed, add — persisted continuity-directive data on `Shot`**

`Task 4`'s `audit_book_continuity` reads `ShotLike.wardrobe`/`.hand_off`/`.beat_index` — these live on the PLANNING-time `ContinuityDirective`/`EventShot` (`event_director.py`), not obviously on the PERSISTED `Shot` DB row (which this plan's research confirmed carries `source_span`/`narration`/`output`/`qa`/`render_mode`/`status`, no `directive` field was found). Run `grep -n "directive\|wardrobe" backend/app/db/models/shot.py` to confirm. If no such field exists: add `continuity_directive: Mapped[dict | None]` (a JSON column, mirroring how `qa` is already stored as a dict) to `Shot`, populate it at shot-creation/render time from the shot's `ContinuityDirective.model_dump()` wherever a `Shot` row is first written or updated with render results (Task 9's worker-dispatch code, and the existing shot-granularity path in `pipeline.py` for non-event shots), and add the matching Alembic migration (same procedure as Task 8 Step 4). Write a small adapter in `review_export.py` (or a shared helper) turning a `(Shot, Beat)` pair into something satisfying Task 4's `ShotLike` Protocol: `beat_index = beat.beat_index`, `wardrobe = (shot.continuity_directive or {}).get("wardrobe")`, `hand_off = (shot.continuity_directive or {}).get("hand_off", "")`, `summary = beat.summary or ""`. Add a unit test for this adapter (a `Shot`+`Beat` pair with a populated `continuity_directive` produces a `ShotLike` with the expected fields) before wiring it into Step 4 below.

- [ ] **Step 2: Write the failing test**

```python
async def test_export_review_includes_numeric_qa_and_repair_action(tmp_path, ...) -> None:
    # existing test fixture setup (a book with shots whose `qa` field is populated)
    ...
    result = await export_book_review(container, book_id, str(tmp_path))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    shot_entry = manifest["shots"][0]
    assert "qa_ccs" in shot_entry
    assert "seam_repair_action" in shot_entry
    assert "long_range_findings" in manifest
```

- [ ] **Step 3: Run to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_cli_integration.py -k numeric_qa -q`
Expected: FAIL — `KeyError`/`assert "qa_ccs" in shot_entry` fails.

- [ ] **Step 4: Implement**

In `export_book_review`, extend each `entries.append({...})` dict with `"qa_ccs": (shot.qa or {}).get("ccs")`, `"qa_style_drift": (shot.qa or {}).get("style_drift")`, `"seam_repair_action": <from Step 1's confirmed source>`. After building `entries`, adapt `ordered_shots` (already sorted in reading order earlier in this function) through Step 1b's `(Shot, Beat) -> ShotLike` adapter, call `report = audit_book_continuity(book_id, adapted_shots)`, and add `manifest["long_range_findings"] = [d.describe() for d in report.drifts]`. Update `_render_html_viewer` to print the numeric scores next to the existing pass/fail badge, and print any long-range findings in a dedicated section at the top of the page.

- [ ] **Step 5: Run to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_cli_integration.py -q`
Expected: PASS.

- [ ] **Step 6: Write `qa_campaign_report.py`**

```python
"""Aggregate all 10 books' export-review manifests into one campaign REPORT.md."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("qa-runs/2026-07-04-10-book-campaign")


def main() -> int:
    rows = []
    for manifest_path in sorted(ROOT.glob("*/manifest.json")):
        m = json.loads(manifest_path.read_text())
        shots = m["shots"]
        accepted = sum(1 for s in shots if s["status"] == "accepted")
        ccs_values = [s["qa_ccs"] for s in shots if s.get("qa_ccs") is not None]
        rows.append({
            "title": m["title"], "shots": len(shots), "accepted": accepted,
            "accept_rate": round(accepted / len(shots), 3) if shots else 0.0,
            "mean_ccs": round(sum(ccs_values) / len(ccs_values), 3) if ccs_values else None,
            "long_range_findings": len(m.get("long_range_findings", [])),
        })
    lines = ["# 10-Book QA Campaign Report", ""]
    lines.append("| Book | Shots | Accepted | Accept Rate | Mean CCS | Long-range findings |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['title']} | {r['shots']} | {r['accepted']} | {r['accept_rate']} | "
            f"{r['mean_ccs']} | {r['long_range_findings']} |"
        )
    (ROOT / "REPORT.md").write_text("\n".join(lines))
    print(f"wrote {ROOT / 'REPORT.md'} covering {len(rows)} books")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Verification gate**

Run: `backend/.venv/bin/pytest tests/test_cli_integration.py -q` → confirm PASS.
Run: `make lint` → confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/cli/actions/review_export.py backend/scripts/qa_campaign_report.py backend/tests/test_cli_integration.py
git commit -m "feat(backend): surface numeric QA scores, repair actions, and long-range findings in review-export"
```

---

## Part C — The campaign itself (operational, not code-TDD; each step has an exact command and expected artifact)

### Task 12: Ingest all 10 books, full text, zero truncation

**Files:** none (operational)

- [ ] **Step 1:** Confirm the stack is up: `docker compose -f infra/docker-compose.yml ps` → all services healthy.
- [ ] **Step 2:** Run `backend/.venv/bin/python backend/scripts/seed_public_domain.py` (Task 5's generalized script) to completion. This is real, slow (real Qwen-VL page analysis per book, potentially 429-prone per the script's own docstring — it already uploads one book at a time, shortest-first is NOT true anymore since Task 5 reordered by campaign relevance, not length; accept the real wall-clock cost).
- [ ] **Step 3:** Confirm: `docker exec kinora-api-1 kinora-admin books list --limit 20` shows all 10 titles with `status=ready` and a REAL page count (not 8) — spot-check at least Count of Monte Cristo and Moby Dick's page counts are in the hundreds, proving full-text ingestion, not the demo truncation.
- [ ] **Step 4:** Record each book's `id` (needed by every subsequent task) in `qa-runs/2026-07-04-10-book-campaign/book-ids.json`.

### Task 13: Pilot — book 1 (Alice's Adventures in Wonderland) end-to-end with the full edge-case matrix

**Files:** creates `qa-runs/2026-07-04-10-book-campaign/alice/`

- [ ] **Step 1:** Set `render_granularity=event` in `backend/.env`; restart the render-worker (`docker compose restart render-worker`).
- [ ] **Step 2:** Drive the actual reading room for Alice via the project's Playwright/chromium against `:5173` (established pattern, see project memory `running-and-verifying-desktop-app`): open the book, scroll through its full length, confirm the film plays continuously with correct scroll-sync throughout. Capture screenshots at open/early-scroll/mid-book/late-book/close.
- [ ] **Step 3:** Exercise each of spec Section 8's edge cases against this book specifically: fast-skim/seek-away, idle-pause/resume, backend-down fallback, budget-low degradation, director-edit repair loop, cross-session preference persistence. Capture one screenshot/recording per edge case into `qa-runs/.../alice/edge-cases/`.
- [ ] **Step 4:** Run `docker exec kinora-api-1 kinora-admin books export-review <alice-book-id> --out /tmp/alice-review` then copy out to `qa-runs/2026-07-04-10-book-campaign/alice/`. Open `index.html` and read through every shot BY EYE — this is the actual accuracy verification, not a formality.
- [ ] **Step 5:** For every real defect found in Step 2-4, write an entry in `qa-runs/.../alice/DEFECTS.md` (seam-level / long-range / pattern-level tier per spec Section 9), fix it, add a regression test per Part A/B's task structure, re-run the affected verification step to confirm the fix.
- [ ] **Step 6:** Apply spec Section 5's root-cause review: read all of Alice's `DEFECTS.md` entries — does any category repeat 2+ times? If so, fix the responsible agent's threshold/prompt/heuristic (not just the individual shots) before moving to Task 14, with its own regression test.
- [ ] **Step 7:** Only once Alice's `make test` + `pnpm typecheck/test/build` are green AND Steps 2-6 found no unresolved defect, mark the pilot clean and proceed to Task 14.

### Task 14: Books 2-10, run individually and sequentially once the pilot is clean

**Correction (2026-07-04, explicit user instruction — overrides the original parallel-dispatch design below):** run these ONE BOOK AT A TIME, not as parallel subagents. Quality control requires reading every book's shots by eye before moving to the next; parallel dispatch would trade that away for speed.

**Files:** creates `qa-runs/2026-07-04-10-book-campaign/<book-slug>/` for each

- [ ] **Step 1:** For each of the remaining 9 books IN SEQUENCE (not parallel), repeat Task 13's Steps 2-5 exactly (same procedure, different book) — finish one book's real defect-fixing and re-verification before starting the next. Skip Step 6 (root-cause review) per-book; run it once collectively in Step 2 below instead.
- [ ] **Step 2:** After all 9 finish, run spec Section 5's root-cause review ONCE across all `DEFECTS.md` files from books 2-10 combined (patterns are more visible across 9 books than 1) — fix any repeated category at the agent level, with regression tests, then re-verify only the specific books whose defects that fix touches.

### Task 15: The one campaign-wide concurrency stress run

**Files:** creates `qa-runs/2026-07-04-10-book-campaign/concurrency-stress/`

- [ ] **Step 1:** Start real reading sessions for 2-3 of the 10 books simultaneously (e.g., separate browser contexts via Playwright, or separate demo-account sessions), all with `KINORA_LIVE_VIDEO` on, scrolling concurrently for several minutes.
- [ ] **Step 2:** Confirm via `docker exec kinora-api-1 kinora-admin books inspect <id>` for each concurrent book, plus Redis budget-ledger inspection, that: no book's budget reservation leaked into another's, no shot was double-rendered (dedup by `shot_hash` held), and the MiniMax USD spend counter (`kinora:minimax:usd_spent`) is a single consistent total across both books, not double-counted or lost.
- [ ] **Step 3:** Capture a screenshot of both sessions playing side-by-side + the Redis/budget inspection output into `qa-runs/.../concurrency-stress/`.

### Task 16: Final report

**Files:** `qa-runs/2026-07-04-10-book-campaign/REPORT.md`, `index.html`

- [ ] **Step 1:** Run `backend/.venv/bin/python backend/scripts/qa_campaign_report.py qa-runs/2026-07-04-10-book-campaign` (Task 11's aggregator).
- [ ] **Step 2:** Manually add to `REPORT.md`: total spend (MiniMax USD counter + ModelScope call count, confirming the $15 cap held), the concurrency-stress verdict (Task 15), and a summary of every pattern-level agent fix made (Section 5) across the whole campaign.
- [ ] **Step 3:** Build the cross-book `index.html` linking each book's own `index.html`.
- [ ] **Step 4:** Confirm the final `.gitignore` state: each book's `clips/*.mp4` are ignored; everything else (`script.md`, `manifest.json`, `index.html`, `DEFECTS.md`, screenshots, recordings, `REPORT.md`) is not.
- [ ] **Step 5:** `make test` (backend) and `pnpm --filter @kinora/desktop run typecheck && test && build` — confirm fully green as the final state, campaign-wide.
- [ ] **Step 6: Commit** (the artifacts, not the venv/node_modules/clips):

```bash
git add qa-runs/
git commit -m "docs: 10-book story-accuracy QA campaign report + artifacts"
```

---

## Execution Log (updated as tasks land)

**Tasks 1-5 complete and reviewed as of this writing** (see `.superpowers/sdd/progress.md` for the authoritative, current ledger — commits, review verdicts, and adjudications). One significant correction made to Task 6 AFTER Tasks 1-5 landed, BEFORE Task 6 was dispatched: the original draft had `LiveEventShotRenderer` build a `WanSpec` directly from raw still-image bytes, but `WanSpec.image_url` is a URL/data-URI string with no bytes field — that draft could never have worked. Corrected to wrap `Generator` (`backend/app/agents/generator.py`) instead of a raw `VideoBackend`, since `Generator.render()` already solves the exact bytes→`WanSpec` translation via `build_wan_spec()`. Caught by tracing `WanSpec`'s real fields and the existing shot-granularity call site (`pipeline.py:664-687`) before dispatch, not discovered mid-implementation. Task 6's plan text above reflects the corrected design.

## Self-Review Notes

- **Spec coverage:** Section 3 (event-wiring) → Tasks 6-10; Section 4 (long-range audit) → Task 4, wired into export at Task 11; Section 5 (root-cause fixing) → Task 13 Step 6 + Task 14 Step 2; Section 6 (multi-provider) → Tasks 1-3; Section 7 (10 books) → Task 5; Section 8 (edge-case matrix) → Task 13 Step 3, repeated per book at Task 14; Section 9 (artifacts) → Task 11 + Tasks 12-16; Section 10 (testing strategy) → every Part A/B task's verification gate; Section 11 (phases) → this document's Part A/B/C ordering; Section 12 (risks) → each risk's mitigation is a specific task step above (e.g., the seek-math risk is Task 10 Step 6's real Playwright verification, run before Task 12's books ever touch it).
- **Update (2026-07-04, during execution):** a follow-up research pass resolved most of the unknowns below against real source before their tasks were dispatched, and caught two real errors in the original draft: Task 6 had invented a nonexistent `CriticVerdict` class (corrected to the real `Verdict`/`RepairAction`/`QARecord` types in `contracts.py`), and Task 3's test referenced a nonexistent public `.backends` attribute on `VideoRouter` (corrected to `.available_backends()`). Also corrected: Task 8's exact file paths (`backend/app/db/models/shot.py`, `backend/app/api/schemas.py` — a single flat file, not a package), Task 9's `QueuedJob` real field list plus the real complication that `shot_ids` is its first list-valued field needing explicit JSON encode/decode (not a copy-paste of an existing scalar field's pattern), Task 11/7's repair-action persistence via the real `DefectRepo.log(...)` pattern (not a bare `Defect(...)` construction), and two file-name corrections (`test_render_event_director.py`/`test_render_continuity_qa.py`, not `test_event_director.py`; `apps/desktop/src/reading/__tests__/timeline.test.ts` already exists). Remaining genuine unknown: ModelScope's exact video endpoint schema (Task 1 — no real token exists yet to probe it, so Task 2 proceeds on the documented image-generation-analog fallback, executed and reviewed — see Task 2's status). Everything else flagged during execution is now fully resolved: `DefectRepo`'s module path, the `apps/desktop/src/reading/` dormant-sprawl question, and `QueuedJob`'s complete enqueue/serialization mechanism (Task 9 Step 1 now has the exact, traced fix — the Lua-script round-trip and `fields: dict[str, str]` shape are fully understood, not partially).
- **Open flag raised during execution — now RESOLVED (2026-07-04):** `apps/desktop/src/reading/{gl,perf,streaming,scrub,offline,gesture}/` (WebGL compositor, adaptive bitrate streaming, service-worker offline cache, etc.) are confirmed **dormant** — all six are re-exported by exactly one file, `apps/desktop/src/reading/playback/index.ts`, itself imported by nothing anywhere in `apps/desktop/src`. That file's own header comment self-identifies as "next-generation... Phase 7 integration... additive over today's FilmPane/useScrollFilm" — the same dormancy shape as the backend's `event_director.py`. The confirmed LIVE import chain for the mounted app is `main.tsx → App.tsx → ReadingRoom.tsx → ScrollFilmEngine.tsx/FilmPane.tsx/useScrollFilm.ts/focusModel.ts/clipCache.ts`, none of which touch the dormant six directories. **Task 10 is correctly scoped as originally written** — no WebGL/adaptive-streaming layer sits between `timelineFromProps` and the real `<video>` element in the live path.
- **Local self-hosted video provider** is deliberately NOT a task in this plan (spec Section 6 demotes it to stretch, post-core) — verified to not exist in the repo, and building a local inference server from scratch is a materially different, larger undertaking than the rest of this plan; revisit only after Task 16 if the campaign's real ModelScope+MiniMax coverage proves insufficient.
