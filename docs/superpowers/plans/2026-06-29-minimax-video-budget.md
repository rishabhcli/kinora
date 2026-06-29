# MiniMax video backend + $30 budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hosted MiniMax (Hailuo) video provider behind a new `video_backend` setting so Kinora can generate a real test film under a hard $30 cap, keeping the existing Wan/DashScope provider as the default.

**Architecture:** A new `MiniMaxVideoProvider` satisfies the existing `VideoBackend` Protocol (`name` / `async render(spec: WanSpec) -> VideoResult` / `async healthy()`), mirroring the Wan `VideoProvider`'s submit→poll→retrieve→download→`record_usage` flow against the MiniMax intl REST API. It is selected by a `video_backend = "dashscope" | "minimax"` branch in `create_providers()`. Spend is capped two ways (belt + suspenders): the existing video-seconds budget (`budget_ceiling_video_s` set to the $30-equivalent), plus a hard, Redis-persisted cumulative-USD guard inside the provider that refuses to submit once the next clip would cross `budget_ceiling_usd`. Phase F is a tiny, user-confirmation-gated live run.

**Tech Stack:** Python 3.11+, FastAPI, httpx, pytest, pydantic-settings

## Global Constraints

- **NO automatic git commits.** The user commits only on explicit instruction. This plan contains NO `git commit` steps. Each task ends with a **verification gate** (run the named tests + `make lint` where noted, confirm PASS, leave changes in the working tree). `git add -A` to stage is allowed; never `git commit`.
- **Real money is at stake.** `KINORA_LIVE_VIDEO` defaults to OFF and MUST stay OFF for all of Phase E (offline, mocked HTTP, zero spend). Only Phase F spends money, and every spending step REQUIRES EXPLICIT USER CONFIRMATION before it runs.
- **Cheapest model only:** `MiniMax-Hailuo-2.3-Fast` @ `768P` / `6s` ≈ `$0.19`/clip. Do NOT use the unverified 512P/$0.08 path.
- **Phase F spend is tiny:** ~$0.20–$1 total (a handful of 6s clips, NOT the whole book). Report tracked spend before AND after every spending step. Never let a run approach $30; turn the gate back OFF when done.
- **The Wan/DashScope provider STAYS.** MiniMax is purely additive (a new branch on `video_backend`); do not modify or remove `app/providers/video.py`'s behaviour.
- **MiniMax URLs expire (~9h).** The provider must download the clip bytes immediately and return them on `VideoResult.clip_bytes`; the render pipeline's existing `_accept` path persists `clip_bytes` to object storage (`keys.clip(...)` + `_put_bytes`) — reuse it, do not add a second persistence path.
- **i2v image rules:** `first_frame_image` must be JPG/JPEG/PNG, aspect ratio between 2:5 and 5:2, short side > 300px, ≤ 20MB. Validate/normalize before submit (Task 4).
- Test runner: `backend/.venv/bin/pytest tests/<path>::<name> -q`. Lint: `make lint` (ruff + mypy) from the repo root.
- Mock HTTP exactly as the existing provider tests do: `httpx.MockTransport(handler)` passed to `ProviderClient(...)`. Never make a real network call in a unit test.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/core/config.py` (modify) | Add the `video_backend` selector, the `minimax_*` provider settings, and `budget_ceiling_usd`. Settings only — no logic. |
| `backend/app/providers/minimax.py` (create) | The `MiniMaxVideoProvider` (a `VideoBackend`): submit t2v/i2v, poll status mapping, retrieve→download_url, download+persist-via-bytes, `LiveVideoDisabled` gating, `record_usage`, the hard USD spend guard. Also the `SpendStore` Protocol + its Redis + in-memory implementations, and the `first_frame_image` validation/normalization helpers. |
| `backend/app/providers/__init__.py` (modify, ~L147) | Branch `create_providers()` on `settings.video_backend`: build `MiniMaxVideoProvider` vs `VideoProvider`. Re-export the new symbols. |
| `backend/tests/test_providers_minimax.py` (create) | Unit tests for the provider: gate, submit bodies (t2v + i2v), poll status mapping, retrieve→download, full success + `record_usage`, USD guard refusal + persistence across instances, image validation. |
| `backend/tests/test_providers_minimax_selection.py` (create) | `create_providers()` selection test + the $30→seconds budget-math test. |

**Decision — HTTP client:** `MiniMaxVideoProvider` reuses a **second `ProviderClient`** built with `base_url_override="https://api.minimax.io/v1"` and `api_key_override=<minimax_api_key>`. Justification: this is the exact pattern the OpenAI reasoning provider already uses in `create_providers()` (`base_url_override` + `api_key_override`), so it inherits the shared retries / circuit breaker / token-bucket rate-limit / structured logging / usage-sink machinery, and stays consistent with the codebase. When `base_url_override` is set, `ProviderClient.base_url` returns that string verbatim (DashScope's `/api/v1` and `/compatible-mode/v1` derivation is bypassed), so the provider builds endpoints directly as `f"{client.base_url}/video_generation"`, etc. The auth header is already `Authorization: Bearer <api_key_override>` via `_auth_headers`. Tests inject `transport=httpx.MockTransport(handler)` into that client, identical to `test_providers_video.py`.

**Decision — spend-guard persistence store:** **Redis**, via a small injectable `SpendStore` Protocol with two methods (`async get_usd() -> float`, `async add_usd(amount: float) -> float`). Justification: Redis is already a first-class dependency (`app/redis/client.py`, the render queue, auth revocation, the flags cache), it survives process restarts (an in-memory counter would reset and a per-process file would not be shared), and `INCRBYFLOAT` is atomic across the separate `api` and `render-worker` processes that can both submit renders. The production implementation (`RedisSpendStore`) wraps `redis.asyncio` `INCRBYFLOAT`/`GET`; tests use an in-memory `InMemorySpendStore` and prove cross-restart persistence by sharing one store instance across two `MiniMaxVideoProvider` instances. When no `SpendStore` is injected, the provider falls back to a process-local in-memory store (correct within one process; the wired path always injects the Redis store).

---

## Task 1: Config additions (`video_backend`, `minimax_*`, `budget_ceiling_usd`)

**Files:**
- Modify: `backend/app/core/config.py` (video block after L86; live-gate region near L193; budget block L172–175)
- Test: `backend/tests/test_providers_minimax_selection.py`

**Interfaces:**
- Consumes: nothing (pure settings).
- Produces: new `Settings` fields used by every later task —
  - `video_backend: str = "dashscope"`  (values: `"dashscope" | "minimax"`)
  - `minimax_api_key: str | None = None`  (env `MINIMAX_API_KEY`, already present in `backend/.env`)
  - `minimax_base_url: str = "https://api.minimax.io/v1"`
  - `minimax_video_model: str = "MiniMax-Hailuo-2.3-Fast"`
  - `minimax_resolution: str = "768P"`
  - `minimax_duration_s: int = 6`
  - `minimax_cost_per_clip_usd: float = 0.19`
  - `budget_ceiling_usd: float = 30.0`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_providers_minimax_selection.py` with the config test (the selection + budget-math tests are added in Task 6; this file starts here):

```python
"""Config + selection + budget-math tests for the MiniMax video backend."""

from __future__ import annotations

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(dashscope_api_key="test", **overrides)  # type: ignore[arg-type]


def test_minimax_config_defaults() -> None:
    s = _settings()
    assert s.video_backend == "dashscope"
    assert s.minimax_api_key is None
    assert s.minimax_base_url == "https://api.minimax.io/v1"
    assert s.minimax_video_model == "MiniMax-Hailuo-2.3-Fast"
    assert s.minimax_resolution == "768P"
    assert s.minimax_duration_s == 6
    assert s.minimax_cost_per_clip_usd == 0.19
    assert s.budget_ceiling_usd == 30.0


def test_minimax_config_overrides() -> None:
    s = _settings(
        video_backend="minimax",
        minimax_api_key="sk-mm",
        minimax_cost_per_clip_usd=0.08,
        budget_ceiling_usd=10.0,
    )
    assert s.video_backend == "minimax"
    assert s.minimax_api_key == "sk-mm"
    assert s.minimax_cost_per_clip_usd == 0.08
    assert s.budget_ceiling_usd == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py -q`
Expected: FAIL — `AttributeError` (e.g. `'Settings' object has no attribute 'video_backend'`).

- [ ] **Step 3: Add the video-backend selector + MiniMax settings**

In `backend/app/core/config.py`, immediately AFTER the Wan video model ids (after L86, the `video_model_r2v` line) and BEFORE the `# --- Wan task polling ---` comment, insert:

```python
    # --- Video backend selection (additive) ---
    # Which hosted video provider the render pipeline uses. "dashscope" keeps the
    # existing Wan provider (default, unchanged); "minimax" selects the cheaper
    # hosted MiniMax (Hailuo) provider. The Wan provider always stays available.
    video_backend: str = "dashscope"  # "dashscope" | "minimax"

    # --- MiniMax (Hailuo) hosted video provider ---
    # The intl host needs no GroupId. Auth is "Authorization: Bearer <key>".
    # MINIMAX_API_KEY is already written to backend/.env (gitignored).
    minimax_api_key: str | None = None
    minimax_base_url: str = "https://api.minimax.io/v1"
    # Cheapest published model @ 768P/6s ≈ $0.19/clip. Do NOT use the unverified
    # 512P/$0.08 path.
    minimax_video_model: str = "MiniMax-Hailuo-2.3-Fast"
    minimax_resolution: str = "768P"
    minimax_duration_s: int = 6
    minimax_cost_per_clip_usd: float = 0.19
```

- [ ] **Step 4: Add the hard USD ceiling to the budget block**

In `backend/app/core/config.py`, in the `# --- Budget (video-seconds) ---` block, immediately AFTER `budget_low_floor_s: float = 120` (L175), insert:

```python
    # Hard USD ceiling for the MiniMax provider's belt-and-suspenders spend guard
    # (kinora.md §11.1). The primary cap is still the video-seconds ledger; this
    # is a second, independent refusal that protects against duration/config drift.
    budget_ceiling_usd: float = 30.0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Verification gate (no commit)**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py -q` → confirm PASS.
Run: `make lint` → confirm ruff + mypy PASS for `backend/app/core/config.py`.
Leave changes in the working tree. `git add -A` is allowed; do NOT commit.

---

## Task 2: SpendStore + USD spend guard (Redis-persisted, with in-memory fallback)

**Files:**
- Create: `backend/app/providers/minimax.py` (the `SpendStore` Protocol, `InMemorySpendStore`, `RedisSpendStore`, and a pure `would_exceed_usd` helper — the provider class itself is Task 3)
- Test: `backend/tests/test_providers_minimax.py`

**Interfaces:**
- Consumes: `Settings.minimax_cost_per_clip_usd`, `Settings.budget_ceiling_usd` (Task 1).
- Produces (used by Task 3):
  - `class SpendStore(Protocol)`: `async def get_usd(self) -> float: ...`; `async def add_usd(self, amount: float) -> float: ...` (returns the new cumulative total).
  - `class InMemorySpendStore` implementing `SpendStore` (a process-local float; thread/async-safe enough for one process).
  - `class RedisSpendStore` implementing `SpendStore` over a `redis.asyncio` client + a key (default `kinora:minimax:usd_spent`), using `INCRBYFLOAT` / `GET`.
  - `def would_exceed_usd(current_usd: float, cost_per_clip_usd: float, ceiling_usd: float) -> bool` (pure: `current_usd + cost_per_clip_usd > ceiling_usd`).
  - `class MiniMaxBudgetExceeded(ProviderError)` raised when the guard refuses (subclass of `app.providers.errors.ProviderError`, non-retryable).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_providers_minimax.py`:

```python
"""Unit tests for the MiniMax (Hailuo) video provider: the spend guard + store,
the LiveVideoDisabled gate, submit bodies (t2v + i2v), poll status mapping,
retrieve→download, a mocked full success path, image validation. No network."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers.minimax import (
    InMemorySpendStore,
    MiniMaxBudgetExceeded,
    would_exceed_usd,
)


# --------------------------------------------------------------------------- #
# Spend guard math + store
# --------------------------------------------------------------------------- #


def test_would_exceed_usd_at_and_below_ceiling() -> None:
    # 157 clips * 0.19 = 29.83 ≤ 30.0 → the 157th is allowed; the 158th crosses.
    assert would_exceed_usd(29.83 - 0.19, 0.19, 30.0) is False  # the 157th
    assert would_exceed_usd(29.83, 0.19, 30.0) is True  # the 158th would be 30.02


async def test_inmemory_spend_store_accumulates() -> None:
    store = InMemorySpendStore()
    assert await store.get_usd() == 0.0
    assert await store.add_usd(0.19) == pytest.approx(0.19)
    assert await store.add_usd(0.19) == pytest.approx(0.38)
    assert await store.get_usd() == pytest.approx(0.38)


def test_minimax_budget_exceeded_is_non_retryable() -> None:
    err = MiniMaxBudgetExceeded("ceiling hit")
    from app.providers.errors import ProviderError

    assert isinstance(err, ProviderError)
    assert err.retryable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.providers.minimax'`.

- [ ] **Step 3: Write the minimal implementation**

Create `backend/app/providers/minimax.py` with the store layer and guard primitives (the provider class is added in Task 3; the file imports are written now so later tasks only append):

```python
"""Hosted MiniMax (Hailuo) video synthesis (async submit → poll → retrieve →
download) with the KINORA_LIVE_VIDEO spend gate and a hard, persistent USD guard.

Mirrors the Wan ``VideoProvider`` contract (``name`` / ``render(WanSpec)`` /
``healthy()``) so it is a drop-in :class:`~app.providers.video_router.VideoBackend`.
It reuses a :class:`~app.providers.base.ProviderClient` configured for the MiniMax
intl host (its own base URL + bearer key), inheriting retries / breaker /
rate-limit / usage accounting. Real renders burn money, so:

* ``render`` raises :class:`~app.providers.errors.LiveVideoDisabled` before any
  network call when ``settings.kinora_live_video`` is off (belt), and
* a persistent cumulative-USD guard refuses to submit once the next clip would
  cross ``settings.budget_ceiling_usd`` (suspenders), independent of the
  video-seconds ledger.

MiniMax retrieve URLs expire (~9h), so the clip bytes are downloaded immediately
and returned on ``VideoResult.clip_bytes``; the render pipeline persists them to
object storage (it never relies on the expiring URL).
"""

from __future__ import annotations

import base64
import binascii
import struct
from typing import Any, Protocol

from .base import ProviderClient
from .base import sdk_get as _get
from .errors import ProviderError
from .types import Usage, VideoResult, WanMode, WanSpec

#: REST paths under ``{minimax_base_url}`` (e.g. https://api.minimax.io/v1).
_SUBMIT_PATH = "video_generation"
_QUERY_PATH = "query/video_generation"
_RETRIEVE_PATH = "files/retrieve"

#: MiniMax task status values.
_STATUS_OK = "Success"
_STATUS_FAIL = "Fail"
_STATUS_PENDING = {"Preparing", "Queueing", "Processing"}

#: Default Redis key for the persistent cumulative-USD spend counter.
_SPEND_KEY = "kinora:minimax:usd_spent"


class MiniMaxBudgetExceeded(ProviderError):  # noqa: N818 - public name in contract
    """Raised when submitting the next clip would cross ``budget_ceiling_usd``.

    A deliberate hard refusal (not a transient fault), so it is non-retryable —
    the router must surface it immediately rather than try another backend.
    """

    retryable = False


def would_exceed_usd(current_usd: float, cost_per_clip_usd: float, ceiling_usd: float) -> bool:
    """True when charging one more clip would push cumulative spend over the cap."""
    return current_usd + cost_per_clip_usd > ceiling_usd


class SpendStore(Protocol):
    """A persistent cumulative-USD counter shared across processes/restarts."""

    async def get_usd(self) -> float:
        """Current cumulative USD spend."""
        ...

    async def add_usd(self, amount: float) -> float:
        """Atomically add ``amount`` USD; return the new cumulative total."""
        ...


class InMemorySpendStore:
    """Process-local :class:`SpendStore` (fallback / tests). Not cross-process."""

    def __init__(self, initial_usd: float = 0.0) -> None:
        self._usd = float(initial_usd)

    async def get_usd(self) -> float:
        return self._usd

    async def add_usd(self, amount: float) -> float:
        self._usd += float(amount)
        return self._usd


class RedisSpendStore:
    """Redis-backed :class:`SpendStore` (production): atomic ``INCRBYFLOAT``.

    Survives restarts and is shared by the separate ``api`` and ``render-worker``
    processes, so neither can independently slip past the USD ceiling.
    """

    def __init__(self, redis: Any, *, key: str = _SPEND_KEY) -> None:
        self._redis = redis
        self._key = key

    async def get_usd(self) -> float:
        raw = await self._redis.get(self._key)
        return float(raw) if raw is not None else 0.0

    async def add_usd(self, amount: float) -> float:
        return float(await self._redis.incrbyfloat(self._key, float(amount)))


__all__ = [
    "InMemorySpendStore",
    "MiniMaxBudgetExceeded",
    "RedisSpendStore",
    "SpendStore",
    "would_exceed_usd",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Verification gate (no commit)**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q` → confirm PASS.
Run: `make lint` → confirm PASS for `backend/app/providers/minimax.py`.
Leave changes in the working tree; do NOT commit.

---

## Task 3: `MiniMaxVideoProvider` — gate, submit, poll, retrieve, download, usage, USD guard

**Files:**
- Modify: `backend/app/providers/minimax.py` (append the provider class; extend `__all__`)
- Test: `backend/tests/test_providers_minimax.py` (append)

**Interfaces:**
- Consumes:
  - `ProviderClient` (Task: existing) — `client.settings`, `client.base_url`, `client.request_json(method, url, *, op, model, json=..., timeout=...)`, `client.download(url, *, op)`, `client.record_usage(Usage(...))`.
  - `SpendStore`, `would_exceed_usd`, `MiniMaxBudgetExceeded` (Task 2).
  - `WanSpec` / `WanMode` / `VideoResult` / `Usage` (existing `app.providers.types`).
  - `LiveVideoDisabled` (existing `app.providers.errors`).
- Produces (used by Tasks 4–6 and the pipeline):
  - `class MiniMaxVideoProvider`:
    - `__init__(self, client: ProviderClient, *, spend_store: SpendStore | None = None, name: str | None = None, poll_interval_s: float = 10.0, poll_timeout_s: float = 600.0) -> None`
    - attribute `name: str` (defaults to `f"minimax:{settings.minimax_video_model}"`)
    - `async def healthy(self) -> bool` (no-network `True` when the gate is off)
    - `async def render(self, spec: WanSpec) -> VideoResult` (raises `LiveVideoDisabled`, then `MiniMaxBudgetExceeded`, then submits)
    - `def _submit_body(self, spec: WanSpec) -> dict[str, Any]` (t2v + i2v shapes)
    - `@staticmethod def _map_status(status: str) -> str` returns `"ok" | "fail" | "pending"`

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_providers_minimax.py`)

```python
# --------------------------------------------------------------------------- #
# Provider: gate, submit bodies, poll mapping, success path, usage, USD guard
# --------------------------------------------------------------------------- #

from app.providers.base import ResilienceConfig
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.types import WanMode, WanSpec

_FAST = ResilienceConfig(
    max_attempts=2,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
    backoff_jitter_s=0.0,
    breaker_failure_threshold=3,
    breaker_recovery_s=0.05,
    rate_per_s=1000.0,
    rate_burst=1000,
)


def _mm_settings(*, live: bool, ceiling_usd: float = 30.0) -> Settings:
    return Settings(
        dashscope_api_key="test",
        kinora_live_video=live,
        video_backend="minimax",
        minimax_api_key="sk-mm",
        budget_ceiling_usd=ceiling_usd,
    )


def _mm_client(handler: object, *, live: bool, ceiling_usd: float = 30.0) -> ProviderClient:
    return ProviderClient(
        _mm_settings(live=live, ceiling_usd=ceiling_usd),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resilience=_FAST,
        base_url_override="https://api.minimax.io/v1",
        api_key_override="sk-mm",
    )


async def test_render_raises_when_live_video_disabled() -> None:
    called = {"hit": False}

    def _tripwire(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        raise AssertionError("MiniMax endpoint must NOT be called when the gate is off")

    client = _mm_client(_tripwire, live=False)
    provider = MiniMaxVideoProvider(client)
    from app.providers.errors import LiveVideoDisabled

    with pytest.raises(LiveVideoDisabled):
        await provider.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow"))
    assert called["hit"] is False
    await client.aclose()


def test_submit_body_text_to_video() -> None:
    client = _mm_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = MiniMaxVideoProvider(client)
    body = provider._submit_body(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn")
    )
    assert body == {
        "model": "MiniMax-Hailuo-2.3-Fast",
        "prompt": "meadow at dawn",
        "duration": 6,
        "resolution": "768P",
    }
    assert "first_frame_image" not in body
    # cleanup is sync-safe; no await needed for the unused client transport


def test_submit_body_image_to_video_sets_first_frame() -> None:
    client = _mm_client(lambda r: httpx.Response(200, json={}), live=True)
    provider = MiniMaxVideoProvider(client)
    body = provider._submit_body(
        WanSpec(
            mode=WanMode.IMAGE_TO_VIDEO,
            prompt="she turns",
            image_url="https://x/first.jpg",
        )
    )
    assert body["first_frame_image"] == "https://x/first.jpg"
    assert body["model"] == "MiniMax-Hailuo-2.3-Fast"


def test_map_status() -> None:
    assert MiniMaxVideoProvider._map_status("Success") == "ok"
    assert MiniMaxVideoProvider._map_status("Fail") == "fail"
    assert MiniMaxVideoProvider._map_status("Preparing") == "pending"
    assert MiniMaxVideoProvider._map_status("Queueing") == "pending"
    assert MiniMaxVideoProvider._map_status("Processing") == "pending"


async def test_render_success_path_downloads_and_records_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/video_generation") and request.method == "POST":
            return httpx.Response(200, json={"task_id": "mm-task-1", "base_resp": {"status_code": 0}})
        if path.endswith("/query/video_generation"):
            assert request.url.params.get("task_id") == "mm-task-1"
            return httpx.Response(200, json={"status": "Success", "file_id": "file-7"})
        if path.endswith("/files/retrieve"):
            assert request.url.params.get("file_id") == "file-7"
            return httpx.Response(
                200, json={"file": {"download_url": "https://cdn.minimax/clip.mp4"}}
            )
        if request.url.host == "cdn.minimax":
            return httpx.Response(200, content=b"MINIMAX-MP4-BYTES")
        return httpx.Response(200, json={})

    client = _mm_client(handler, live=True)
    store = InMemorySpendStore()
    provider = MiniMaxVideoProvider(
        client, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0
    )
    result = await provider.render(
        WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="meadow at dawn")
    )
    assert result.clip_bytes == b"MINIMAX-MP4-BYTES"
    assert result.clip_url == "https://cdn.minimax/clip.mp4"
    assert result.provider_task_id == "mm-task-1"
    assert result.duration_s == 6.0
    assert result.model == "MiniMax-Hailuo-2.3-Fast"
    # video-seconds recorded for the primary budget path
    totals = client.usage_totals
    assert totals is not None and totals.video_seconds == 6.0
    # USD spend persisted
    assert await store.get_usd() == pytest.approx(0.19)
    await client.aclose()


async def test_render_refuses_past_usd_ceiling_and_persists_across_instances() -> None:
    submits = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/video_generation") and request.method == "POST":
            submits["count"] += 1
            return httpx.Response(200, json={"task_id": "t", "base_resp": {"status_code": 0}})
        if path.endswith("/query/video_generation"):
            return httpx.Response(200, json={"status": "Success", "file_id": "f"})
        if path.endswith("/files/retrieve"):
            return httpx.Response(200, json={"file": {"download_url": "https://cdn.minimax/c.mp4"}})
        if request.url.host == "cdn.minimax":
            return httpx.Response(200, content=b"X")
        return httpx.Response(200, json={})

    # Ceiling 0.30 → one 0.19 clip allowed; the second (0.38) is refused.
    store = InMemorySpendStore()
    client1 = _mm_client(handler, live=True, ceiling_usd=0.30)
    p1 = MiniMaxVideoProvider(client1, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0)
    await p1.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="one"))
    assert submits["count"] == 1
    assert await store.get_usd() == pytest.approx(0.19)
    await client1.aclose()

    # A NEW provider instance (simulating a restart) shares the persisted store
    # and refuses BEFORE any submit.
    client2 = _mm_client(handler, live=True, ceiling_usd=0.30)
    p2 = MiniMaxVideoProvider(client2, spend_store=store, poll_interval_s=0.0, poll_timeout_s=5.0)
    with pytest.raises(MiniMaxBudgetExceeded):
        await p2.render(WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="two"))
    assert submits["count"] == 1  # no second submission happened
    await client2.aclose()


async def test_healthy_is_true_without_network_when_gate_off() -> None:
    def _tripwire(request: httpx.Request) -> httpx.Response:
        raise AssertionError("healthy() must not call the network when gated off")

    client = _mm_client(_tripwire, live=False)
    provider = MiniMaxVideoProvider(client)
    assert await provider.healthy() is True
    assert provider.name == "minimax:MiniMax-Hailuo-2.3-Fast"
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: FAIL — `ImportError: cannot import name 'MiniMaxVideoProvider'`.

- [ ] **Step 3: Write the minimal implementation** (append the provider class to `backend/app/providers/minimax.py`, ABOVE the `__all__` block, and extend `__all__`)

```python
import asyncio
import time


class MiniMaxVideoProvider:
    """Hosted MiniMax (Hailuo) render client (gated + USD-capped).

    Satisfies the :class:`~app.providers.video_router.VideoBackend` protocol
    (``name`` / ``render`` / ``healthy``) so it is a drop-in alternative to the
    Wan :class:`~app.providers.video.VideoProvider`.
    """

    def __init__(
        self,
        client: ProviderClient,
        *,
        spend_store: SpendStore | None = None,
        name: str | None = None,
        poll_interval_s: float = 10.0,
        poll_timeout_s: float = 600.0,
    ) -> None:
        self._client = client
        self._settings = client.settings
        self._spend = spend_store or InMemorySpendStore()
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self.name = name or f"minimax:{self._settings.minimax_video_model}"

    # -- liveness (no render spend) -------------------------------------- #

    async def healthy(self) -> bool:
        """Cheap probe: no network when the live gate is off (gate ≠ fault)."""
        return True

    # -- request shape --------------------------------------------------- #

    def _submit_body(self, spec: WanSpec) -> dict[str, Any]:
        """Translate a :class:`WanSpec` into the MiniMax submit JSON.

        TEXT_TO_VIDEO → {model, prompt, duration, resolution}. Image-conditioned
        modes add ``first_frame_image`` (a public URL or a ``data:`` URI). All
        non-t2v modes map to image-to-video using the spec's first available
        image input (MiniMax has no multi-reference / first-last / continuation
        protocol here).
        """
        s = self._settings
        body: dict[str, Any] = {
            "model": s.minimax_video_model,
            "prompt": spec.prompt or "",
            "duration": s.minimax_duration_s,
            "resolution": s.minimax_resolution,
        }
        if spec.mode is not WanMode.TEXT_TO_VIDEO:
            first = self._first_frame(spec)
            if first is None:
                from .errors import ProviderBadRequest

                raise ProviderBadRequest(
                    f"MiniMax {spec.mode.value} render has no first_frame_image input"
                )
            body["first_frame_image"] = normalize_first_frame_image(first)
        return body

    @staticmethod
    def _first_frame(spec: WanSpec) -> str | None:
        """Pick the single conditioning image for image-to-video, by mode."""
        if spec.image_url:
            return spec.image_url
        if spec.first_frame_url:
            return spec.first_frame_url
        if spec.reference_image_urls:
            return spec.reference_image_urls[0]
        return None

    @staticmethod
    def _map_status(status: str) -> str:
        if status == _STATUS_OK:
            return "ok"
        if status == _STATUS_FAIL:
            return "fail"
        return "pending"

    # -- render (GATED + USD-CAPPED) ------------------------------------- #

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit a real MiniMax render, poll, retrieve, download, and return it.

        Order of guards (cheapest/most-deliberate first, no spend until the last):
        1. ``LiveVideoDisabled`` when ``kinora_live_video`` is off (no network).
        2. ``MiniMaxBudgetExceeded`` when the next clip would cross the USD cap.
        Only then is the task submitted.
        """
        if not self._settings.kinora_live_video:
            from .errors import LiveVideoDisabled

            raise LiveVideoDisabled(
                "live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                "no MiniMax task submitted",
            )

        cost = float(self._settings.minimax_cost_per_clip_usd)
        current = await self._spend.get_usd()
        if would_exceed_usd(current, cost, float(self._settings.budget_ceiling_usd)):
            raise MiniMaxBudgetExceeded(
                f"MiniMax USD ceiling would be exceeded: spent ${current:.2f} "
                f"+ ${cost:.2f} > cap ${self._settings.budget_ceiling_usd:.2f}; "
                "refusing to submit",
            )

        task_id = await self._submit(spec)
        # Charge the USD spend as soon as the task is accepted (it is now billable);
        # the video-seconds ledger is charged via record_usage below.
        await self._spend.add_usd(cost)

        file_id = await self._poll_to_completion(task_id)
        download_url = await self._retrieve_download_url(file_id)
        clip_bytes = await self._client.download(download_url, op="video")

        duration = float(self._settings.minimax_duration_s)
        self._client.record_usage(
            Usage(
                model=self._settings.minimax_video_model,
                operation="video",
                video_seconds=duration,
                request_id=task_id,
            )
        )
        return VideoResult(
            duration_s=duration,
            model=self._settings.minimax_video_model,
            mode=spec.mode,
            provider_task_id=task_id,
            clip_url=download_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
        )

    async def _submit(self, spec: WanSpec) -> str:
        body = await asyncio.to_thread(self._submit_body, spec) if False else self._submit_body(spec)
        result = await self._client.request_json(
            "POST",
            f"{self._client.base_url}/{_SUBMIT_PATH}",
            op="minimax_video_submit",
            model=self._settings.minimax_video_model,
            json=body,
        )
        task_id = _get(result, "task_id")
        if not task_id:
            raise ProviderError(
                "MiniMax submission returned no task_id",
                request_id=str(_get(_get(result, "base_resp"), "status_code") or ""),
            )
        return str(task_id)

    async def _poll_to_completion(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout_s
        while True:
            result = await self._client.request_json(
                "GET",
                f"{self._client.base_url}/{_QUERY_PATH}",
                op="minimax_video_poll",
                model=self._settings.minimax_video_model,
                params={"task_id": task_id},
            )
            mapped = self._map_status(str(_get(result, "status") or ""))
            if mapped == "ok":
                file_id = _get(result, "file_id")
                if not file_id:
                    raise ProviderError(
                        "MiniMax task succeeded but returned no file_id",
                        request_id=task_id,
                    )
                return str(file_id)
            if mapped == "fail":
                raise ProviderError(
                    f"MiniMax task {task_id} ended Fail", request_id=task_id
                )
            if time.monotonic() >= deadline:
                from .errors import ProviderTimeout

                raise ProviderTimeout(
                    f"MiniMax task {task_id} did not complete within {self._poll_timeout_s}s",
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _retrieve_download_url(self, file_id: str) -> str:
        result = await self._client.request_json(
            "GET",
            f"{self._client.base_url}/{_RETRIEVE_PATH}",
            op="minimax_file_retrieve",
            model=self._settings.minimax_video_model,
            params={"file_id": file_id},
        )
        url = _get(_get(result, "file"), "download_url")
        if not url:
            raise ProviderError(
                "MiniMax file retrieve returned no download_url", request_id=file_id
            )
        return str(url)
```

> **NOTE on `_submit` line:** the `... if False else ...` expression above is a copy artifact — write it simply as `body = self._submit_body(spec)`. (Stated explicitly so the implementer does not transcribe the artifact.)

Extend `__all__` in `backend/app/providers/minimax.py` to include the provider and the normalization helper added in Task 4:

```python
__all__ = [
    "InMemorySpendStore",
    "MiniMaxBudgetExceeded",
    "MiniMaxVideoProvider",
    "RedisSpendStore",
    "SpendStore",
    "normalize_first_frame_image",
    "validate_first_frame_image",
    "would_exceed_usd",
]
```

> **IMPORTANT — `params=` on `ProviderClient.request_json`:** the current `ProviderClient.request_json` signature is `(method, url, *, op, model, json=None, headers=None, timeout=None)` — it does **not** accept `params`. Task 3 Step 3a (below) adds `params` support to `request_json` so the GET poll/retrieve calls can pass query strings cleanly. Do this BEFORE running the tests.

- [ ] **Step 3a: Add `params=` support to `ProviderClient.request_json`**

In `backend/app/providers/base.py`, modify `request_json` (currently L446–475) to accept and forward `params`:

Change the signature from:

```python
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        op: str,
        model: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Resilient JSON request; raises typed errors on non-2xx / bad bodies."""
        call_timeout = timeout or self.config.default_timeout_s

        async def attempt() -> dict[str, Any]:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    json=json,
                    headers=self._auth_headers(headers),
                    timeout=call_timeout,
                )
```

to:

```python
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        op: str,
        model: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Resilient JSON request; raises typed errors on non-2xx / bad bodies."""
        call_timeout = timeout or self.config.default_timeout_s

        async def attempt() -> dict[str, Any]:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._auth_headers(headers),
                    timeout=call_timeout,
                )
```

(Only the `params` parameter and the `params=params` forward are added; everything else is unchanged. This is additive and back-compatible — every existing caller omits `params`.)

- [ ] **Step 3b: Apply the `_submit` fix**

Edit the `_submit` method body so its first line reads exactly:

```python
        body = self._submit_body(spec)
```

(remove the `if False else` artifact).

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: PASS — all tests in the file pass (the image-validation tests are added in Task 4; `normalize_first_frame_image` is referenced but not yet defined, so add a temporary stub IF Task 4 is not done in the same session). To keep Task 3 self-contained, add this minimal passthrough now at the top of `minimax.py` (Task 4 replaces it with the real validator):

```python
def validate_first_frame_image(image: str) -> None:
    """Validate a MiniMax first_frame_image (URL or data URI). Replaced in Task 4."""
    return None


def normalize_first_frame_image(image: str) -> str:
    """Return a MiniMax-acceptable first_frame_image. Replaced in Task 4."""
    return image
```

Re-run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: PASS.

- [ ] **Step 5: Verification gate (no commit)**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py tests/test_providers_video.py tests/test_providers_base.py -q` → confirm PASS (the existing Wan + base tests must stay green after the `params=` change).
Run: `make lint` → confirm PASS.
Leave changes in the working tree; do NOT commit.

---

## Task 4: `first_frame_image` validation / normalization (i2v image rules)

**Files:**
- Modify: `backend/app/providers/minimax.py` (replace the Task-3 stubs `validate_first_frame_image` / `normalize_first_frame_image` with real implementations)
- Test: `backend/tests/test_providers_minimax.py` (append)

**Interfaces:**
- Consumes: nothing new (pure functions over a `str` image reference + raw bytes).
- Produces (already referenced by `_submit_body` in Task 3):
  - `def validate_first_frame_image(image: str) -> None` — raises `ProviderBadRequest` when a `data:` image violates MiniMax rules (not JPG/PNG, short side ≤ 300px, aspect outside 2:5..5:2, > 20MB). HTTP/HTTPS URLs are passed through unvalidated (MiniMax fetches them; we cannot read remote dimensions cheaply).
  - `def normalize_first_frame_image(image: str) -> str` — calls `validate_first_frame_image` and returns the image unchanged (URLs and already-valid data URIs); the function exists as the single submit-time choke point.
  - `def _decode_data_uri(image: str) -> tuple[str, bytes] | None` — `("image/jpeg", raw)` for a `data:` URI, else `None`.
  - `def _image_dimensions(raw: bytes) -> tuple[int, int] | None` — `(width, height)` for PNG/JPEG headers, else `None`.

Rules (verbatim from the spec/risks): JPG/JPEG/PNG only; aspect ratio between 2:5 (0.4) and 5:2 (2.5); short side > 300px; ≤ 20MB.

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_providers_minimax.py`)

```python
# --------------------------------------------------------------------------- #
# first_frame_image validation
# --------------------------------------------------------------------------- #

from app.providers.errors import ProviderBadRequest
from app.providers.minimax import (
    normalize_first_frame_image,
    validate_first_frame_image,
)


def _png_data_uri(width: int, height: int) -> str:
    """A minimal valid PNG header (IHDR with width/height) as a data URI.

    Only the 24-byte signature+IHDR prefix is needed for dimension parsing; the
    rest is padding so the byte length is realistic but small.
    """
    import base64
    import struct

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    raw = sig + ihdr_len + ihdr + b"\x00" * 64
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_validate_passes_http_url_unchanged() -> None:
    # We cannot read remote dimensions cheaply; URLs pass through.
    validate_first_frame_image("https://x/first.jpg")  # no raise
    assert normalize_first_frame_image("https://x/first.jpg") == "https://x/first.jpg"


def test_validate_passes_valid_png_data_uri() -> None:
    uri = _png_data_uri(800, 600)  # 4:3, short side 600 > 300
    validate_first_frame_image(uri)  # no raise
    assert normalize_first_frame_image(uri) == uri


def test_validate_rejects_short_side_too_small() -> None:
    uri = _png_data_uri(800, 200)  # short side 200 ≤ 300
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image(uri)


def test_validate_rejects_aspect_ratio_out_of_range() -> None:
    uri = _png_data_uri(2000, 400)  # 5:1 aspect → 5.0 > 2.5
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image(uri)


def test_validate_rejects_non_image_data_uri() -> None:
    with pytest.raises(ProviderBadRequest):
        validate_first_frame_image("data:text/plain;base64,aGVsbG8=")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q -k validate`
Expected: FAIL — the stub `validate_first_frame_image` never raises, so the three "rejects" tests fail (e.g. `DID NOT RAISE ProviderBadRequest`).

- [ ] **Step 3: Replace the stubs with real implementations**

In `backend/app/providers/minimax.py`, replace the two stub functions (`validate_first_frame_image`, `normalize_first_frame_image`) from Task 3 with:

```python
#: MiniMax first_frame_image limits.
_MM_MAX_BYTES = 20 * 1024 * 1024
_MM_MIN_SHORT_SIDE = 300
_MM_MIN_ASPECT = 2 / 5  # 0.4
_MM_MAX_ASPECT = 5 / 2  # 2.5
_MM_ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png"}


def _decode_data_uri(image: str) -> tuple[str, bytes] | None:
    """Return ``(mime, raw_bytes)`` for a ``data:`` URI, else ``None``."""
    if not image.startswith("data:"):
        return None
    try:
        header, b64 = image[len("data:") :].split(",", 1)
    except ValueError:
        return None
    mime = header.split(";", 1)[0].strip().lower()
    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, binascii.Error):
        return None
    return mime, raw


def _image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Parse ``(width, height)`` from a PNG or JPEG header, else ``None``."""
    # PNG: signature + IHDR holds width/height as big-endian uint32 at offset 16.
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return int(width), int(height)
    # JPEG: walk the marker segments to the first SOF (Start Of Frame).
    if raw[:2] == b"\xff\xd8":
        i = 2
        n = len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            # SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15 carry dimensions.
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = struct.unpack(">H", raw[i + 5 : i + 7])[0]
                width = struct.unpack(">H", raw[i + 7 : i + 9])[0]
                return int(width), int(height)
            seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
            i += 2 + seg_len
    return None


def validate_first_frame_image(image: str) -> None:
    """Validate a MiniMax ``first_frame_image`` against the documented rules.

    Rules: JPG/JPEG/PNG; short side > 300px; aspect ratio in [2:5, 5:2]; ≤ 20MB.
    HTTP(S) URLs pass through (MiniMax fetches them; remote dimensions are not
    read here). Only ``data:`` URIs are inspected locally.
    """
    if image.startswith(("http://", "https://")):
        return
    decoded = _decode_data_uri(image)
    if decoded is None:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            "MiniMax first_frame_image must be an http(s) URL or a base64 data URI"
        )
    mime, raw = decoded
    if mime not in _MM_ALLOWED_MIME:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image must be JPG/JPEG/PNG, got {mime!r}"
        )
    if len(raw) > _MM_MAX_BYTES:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image exceeds 20MB ({len(raw)} bytes)"
        )
    dims = _image_dimensions(raw)
    if dims is None:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest("MiniMax first_frame_image dimensions could not be parsed")
    width, height = dims
    if min(width, height) <= _MM_MIN_SHORT_SIDE:
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image short side must be > {_MM_MIN_SHORT_SIDE}px "
            f"(got {width}x{height})"
        )
    aspect = width / height if height else 0.0
    if not (_MM_MIN_ASPECT <= aspect <= _MM_MAX_ASPECT):
        from .errors import ProviderBadRequest

        raise ProviderBadRequest(
            f"MiniMax first_frame_image aspect ratio must be in [2:5, 5:2] "
            f"(got {width}x{height} = {aspect:.2f})"
        )


def normalize_first_frame_image(image: str) -> str:
    """Validate and return a MiniMax-acceptable ``first_frame_image`` (the single
    submit-time choke point). Today validation-only; future normalization (e.g.
    transcoding a WEBP keyframe to JPEG) would happen here."""
    validate_first_frame_image(image)
    return image
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q`
Expected: PASS (all provider + validation tests).

- [ ] **Step 5: Verification gate (no commit)**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax.py -q` → confirm PASS.
Run: `make lint` → confirm PASS (mypy must be happy with the `struct` / `base64` / `binascii` usage; no `# type: ignore` is needed since `binascii.Error` is imported directly).
Leave changes in the working tree; do NOT commit.

---

## Task 5: Wire selection in `create_providers()` + Redis spend store

**Files:**
- Modify: `backend/app/providers/__init__.py` (the `video = VideoProvider(client)` line ~L147; the imports near L60; `__all__`)
- Test: `backend/tests/test_providers_minimax_selection.py` (append)

**Interfaces:**
- Consumes: `Settings.video_backend`, `Settings.minimax_*` (Task 1); `MiniMaxVideoProvider`, `RedisSpendStore` (Tasks 2–4); `ProviderClient` with `base_url_override` / `api_key_override` (existing).
- Produces:
  - `create_providers(settings)` now returns `Providers` whose `.video` is a `MiniMaxVideoProvider` when `settings.video_backend == "minimax"` (else the existing `VideoProvider`).
  - A `build_minimax_video_provider(settings, *, usage_sink) -> MiniMaxVideoProvider` helper (so the wiring is testable in isolation and reused by Phase F).

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_providers_minimax_selection.py`)

```python
from app.providers import create_providers
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.video import VideoProvider


def test_create_providers_default_is_wan() -> None:
    providers = create_providers(_settings())
    assert isinstance(providers.video, VideoProvider)


def test_create_providers_minimax_backend_selected() -> None:
    providers = create_providers(
        _settings(video_backend="minimax", minimax_api_key="sk-mm")
    )
    assert isinstance(providers.video, MiniMaxVideoProvider)
    assert providers.video.name == "minimax:MiniMax-Hailuo-2.3-Fast"


def test_minimax_backend_without_key_falls_back_to_wan() -> None:
    # Misconfiguration guard: selecting minimax with no key must not crash the
    # whole provider bundle at construction; fall back to Wan (which still gates
    # spend) and let preflight surface the missing key.
    providers = create_providers(_settings(video_backend="minimax", minimax_api_key=None))
    assert isinstance(providers.video, VideoProvider)


def test_budget_seconds_equivalent_of_thirty_dollars() -> None:
    # $30 / $0.19 per clip * 6s/clip ≈ 947s ≈ 157 clips. Assert the documented
    # mapping the operator sets BUDGET_CEILING_VIDEO_S to for a MiniMax run.
    s = _settings()
    clips = s.budget_ceiling_usd / s.minimax_cost_per_clip_usd
    seconds = clips * s.minimax_duration_s
    assert round(clips) == 157
    assert round(seconds) == 947
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py -q`
Expected: FAIL — `test_create_providers_minimax_backend_selected` fails (`.video` is a `VideoProvider`, not a `MiniMaxVideoProvider`).

- [ ] **Step 3: Add the selection branch + import**

In `backend/app/providers/__init__.py`, add an import next to the existing video import (after the `from .video import VideoPollConfig, VideoProvider` line, ~L60):

```python
from .minimax import MiniMaxVideoProvider, RedisSpendStore
```

Add a module-level helper just below the `create_video_router` function (after L192) so the wiring is reusable and testable:

```python
def build_minimax_video_provider(
    settings: Settings,
    *,
    usage_sink: UsageSink | None = None,
) -> MiniMaxVideoProvider:
    """Build a MiniMax video backend on its own MiniMax-configured client.

    Uses ``base_url_override`` + ``api_key_override`` so the provider reuses the
    shared :class:`ProviderClient` resilience (retries/breaker/rate-limit) and the
    one usage sink (unified cost/budget), exactly like the OpenAI reasoning path.
    A Redis-backed :class:`RedisSpendStore` persists the cumulative-USD guard
    across restarts and across the api/render-worker processes.
    """
    mm_client = ProviderClient(
        settings,
        usage_sink=usage_sink,
        base_url_override=settings.minimax_base_url,
        api_key_override=settings.minimax_api_key,
    )
    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    spend_store = RedisSpendStore(redis)
    return MiniMaxVideoProvider(mm_client, spend_store=spend_store)
```

Then change the single video-construction line in `create_providers` (L147) FROM:

```python
    video = VideoProvider(client)
```

TO:

```python
    video: VideoProvider | MiniMaxVideoProvider
    if resolved.video_backend.lower() == "minimax" and resolved.minimax_api_key:
        # Share the main client's usage sink so MiniMax video-seconds land in the
        # same budget accounting as everything else.
        video = build_minimax_video_provider(resolved, usage_sink=client.usage_sink)
    else:
        video = VideoProvider(client)
```

Add `MiniMaxVideoProvider`, `RedisSpendStore`, and `build_minimax_video_provider` to `__all__` in `backend/app/providers/__init__.py` (alphabetical insert).

> **Type note:** `Providers.video` is declared `video: VideoProvider` in the frozen dataclass (L88). Widen it to `video: VideoProvider | MiniMaxVideoProvider` so mypy accepts the MiniMax instance. Both already satisfy the `VideoBackend` Protocol the pipeline/generator consume, so no downstream signature changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py -q`
Expected: PASS (all selection + budget-math tests).

- [ ] **Step 5: Verification gate (no commit)**

Run: `backend/.venv/bin/pytest tests/test_providers_minimax_selection.py tests/test_providers_minimax.py tests/test_agents_generator.py -q` → confirm PASS (the generator's `VideoBackend` protocol tests must still pass).
Run: `make lint` → confirm PASS (especially the `Providers.video` union widening and the local `Redis` import).
Leave changes in the working tree; do NOT commit.

---

## Task 6: Full offline regression + documentation of the $30 operating procedure

**Files:**
- Modify: `.env.example` (document the MiniMax + USD-cap knobs near the existing video block)
- Test: (regression only — no new test file)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: an operator-facing `.env.example` block documenting how to select MiniMax and set the dual cap. No code.

- [ ] **Step 1: Document the knobs in `.env.example`**

In `.env.example`, immediately AFTER the existing `VIDEO_POLL_MAX_INTERVAL_S=15` line (L53), insert:

```bash

# --- Video backend selection (additive) ---
# "dashscope" (default, Wan) or "minimax" (cheaper hosted MiniMax/Hailuo).
VIDEO_BACKEND=dashscope
# MiniMax (Hailuo) hosted provider. The intl host needs no GroupId.
# MINIMAX_API_KEY is set in backend/.env (gitignored), not here.
MINIMAX_BASE_URL=https://api.minimax.io/v1
MINIMAX_VIDEO_MODEL=MiniMax-Hailuo-2.3-Fast
MINIMAX_RESOLUTION=768P
MINIMAX_DURATION_S=6
MINIMAX_COST_PER_CLIP_USD=0.19
# Hard USD ceiling for the MiniMax spend guard (belt + suspenders with the
# seconds ledger). For a $30 MiniMax run also set the seconds ledger to the
# $30-equivalent: BUDGET_CEILING_VIDEO_S ≈ 30 / 0.19 * 6 ≈ 947 (~157 clips).
BUDGET_CEILING_USD=30.0
```

- [ ] **Step 2: Full offline regression — provider + pipeline + budget suites**

Run:
```
backend/.venv/bin/pytest tests/test_providers_minimax.py tests/test_providers_minimax_selection.py tests/test_providers_video.py tests/test_providers_video_router.py tests/test_providers_base.py tests/test_agents_generator.py tests/test_memory_budget.py tests/test_render_pipeline.py -q
```
Expected: PASS (all). This proves MiniMax is additive — the Wan, router, base-client, generator, budget, and pipeline suites are all still green.

- [ ] **Step 3: Lint gate**

Run: `make lint`
Expected: ruff + mypy PASS across the changed files (`config.py`, `providers/base.py`, `providers/minimax.py`, `providers/__init__.py`).

- [ ] **Step 4: Verification gate (no commit)**

Confirm Steps 2 and 3 PASS. Leave all changes in the working tree. `git add -A` is allowed; do NOT commit. **End of Phase E.**

---

## Phase F — Gated live test video (REAL MONEY — EXPLICIT USER CONFIRMATION REQUIRED AT EVERY SPENDING STEP)

> **STOP. Read before doing anything in Phase F.**
> - Phase F is the ONLY phase that spends real money. Phases A–E must be merged and verified offline first.
> - **Before each step marked `[SPEND]`, you MUST obtain explicit user confirmation in this session.** If the user has not said "yes, proceed with the live MiniMax run" for that specific step, STOP and ask.
> - Keep total spend to ~$0.20–$1 (a handful of 6s clips at $0.19 each — i.e. roughly 1 to 5 clips). NEVER the whole book. NEVER approach $30.
> - Report the tracked USD spend BEFORE and AFTER every `[SPEND]` step (read the Redis counter — Step 4).
> - Turn `KINORA_LIVE_VIDEO` back to `0` the moment the test is done (Step 8).
> - The cheapest model (`MiniMax-Hailuo-2.3-Fast` @ 768P/6s) is already the default; do not change it.

**Files:** none created. This phase runs commands against a live stack and `backend/.env`.

**Interfaces:**
- Consumes: the Phase-E provider + selection; the existing render pipeline (`build_render_pipeline` → `_render_live_loop` → `generator.render` → reserve/commit), which already persists `output.clip_bytes` to object storage in `_accept` (`keys.clip` + `_put_bytes`). MiniMax returns `clip_bytes`, so the existing persistence path is reused unchanged.

- [ ] **Step 1: Confirm preconditions (NO SPEND)**

- Confirm `MINIMAX_API_KEY` is present in `backend/.env` (do NOT print the value):
  Run: `grep -q '^MINIMAX_API_KEY=' backend/.env && echo "MINIMAX_API_KEY present" || echo "MISSING"`
  Expected: `MINIMAX_API_KEY present`.
- Confirm the stack is up (Postgres on 5433, redis, minio, api, render-worker). If not, bring it up per the README (`make stack-up`). Do NOT seed — books are already seeded (81 books).
- Confirm 81 books are seeded and pick a target book id (NO SPEND — read-only):
  Run: `curl -s http://localhost:8000/api/library/books -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -40`
  (Obtain `$TOKEN` via the demo login `demo@kinora.local` / `demo-password-123` per the README; this is a read-only call.)

- [ ] **Step 2: Set the dual cap + select MiniMax in `backend/.env` (NO SPEND — config only)**

Edit `backend/.env` to add/confirm (do NOT enable the live gate yet):
```
VIDEO_BACKEND=minimax
BUDGET_CEILING_USD=1.00
BUDGET_CEILING_VIDEO_S=30
BUDGET_PER_SESSION_S=30
BUDGET_PER_SCENE_S=30
```
Rationale: a $1.00 USD hard cap (≈ 5 clips) AND a 30 video-second seconds ceiling (≈ 5 × 6s) — both well under $30. This is belt + suspenders for the test itself. `KINORA_LIVE_VIDEO` stays `false` for now.

- [ ] **Step 3: Reset the persistent USD spend counter to a known baseline (NO SPEND)**

Run: `docker compose -f infra/docker-compose.yml exec redis redis-cli SET kinora:minimax:usd_spent 0`
Expected: `OK`. (This zeroes the cumulative-USD guard so the before/after report is clean. Use the redis service name from `infra/docker-compose.yml`; adjust if the compose file differs.)

- [ ] **Step 4: Report spend BEFORE (NO SPEND)**

Run: `docker compose -f infra/docker-compose.yml exec redis redis-cli GET kinora:minimax:usd_spent`
Expected: `0` (or `0.0`). **Record this value.**

- [ ] **Step 5: `[SPEND]` Enable the live gate and generate a SINGLE clip — REQUIRES EXPLICIT USER CONFIRMATION**

> **Ask the user now:** "Phase F Step 5 will spend real money — approximately **$0.19** for one 6-second MiniMax clip. The USD cap is $1.00 and the seconds cap is 30s. Do you want me to proceed?" Wait for an explicit "yes" before running anything below.

After confirmation:
1. Set `KINORA_LIVE_VIDEO=true` in `backend/.env` and restart the api + render-worker so they reload settings:
   Run: `docker compose -f infra/docker-compose.yml restart api render-worker`
2. Enqueue ONE shot render for the chosen book (a single 6s clip). Use the existing render path by enqueuing a single shot job (the pipeline reserves budget → `MiniMaxVideoProvider.render` → submit/poll/retrieve → download → `_accept` persists to object storage). The exact enqueue command depends on the queue CLI; the minimal, scoped approach is a one-shot Python invocation inside the api container that renders exactly one shot:
   Run:
   ```
   docker compose -f infra/docker-compose.yml exec api python -m app.cli.render_one_shot --book <BOOK_ID> --max-shots 1
   ```
   > If `app.cli.render_one_shot` does not exist, do NOT write a broad batch script. Instead, render exactly one shot via a tiny inline script that calls `build_render_pipeline(...).render_shot(book_id, shot_id)` for a single known `shot_id` from the chosen book, then exits. Keep it to ONE shot.
3. Watch the logs for the MiniMax submit/poll and the `degrade.shipped` vs `cache.hit`/accept path:
   Run: `docker compose -f infra/docker-compose.yml logs --tail=80 render-worker api | grep -iE "minimax|video_seconds|clip|degrade|accept"`

- [ ] **Step 6: `[SPEND-VERIFY]` Report spend AFTER + verify persistence and retrievability (NO ADDITIONAL SPEND)**

1. Report spend AFTER:
   Run: `docker compose -f infra/docker-compose.yml exec redis redis-cli GET kinora:minimax:usd_spent`
   Expected: `0.19` (one clip). **Record this value; confirm the delta is exactly one clip (~$0.19) and the value is far below $1.00 and $30.**
2. Verify the clip persisted to object storage (MinIO). The accepted shot's `output.clip_key` is `keys.clip(book_id, shot_id)`. List the book's clip objects:
   Run: `docker compose -f infra/docker-compose.yml exec minio mc ls --recursive local/kinora/ 2>/dev/null | grep -i "<BOOK_ID>" | grep -i clip` (or use the project's object-store inspection path / the `/api/films` endpoint).
   Expected: at least one `.mp4` object under the book's prefix.
3. Verify retrievability: fetch the persisted clip's presigned URL via the films API and confirm a 200 + non-zero `Content-Length`:
   Run: `curl -sI "$(curl -s http://localhost:8000/api/films/<BOOK_ID> -H "Authorization: Bearer $TOKEN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["shots"][0]["clip_url"])')" | head -5`
   Expected: `HTTP/1.1 200` and a non-zero `Content-Length`. (Field names may differ; adapt to the real `/api/films` schema — the goal is: the persisted clip is downloadable.)

- [ ] **Step 7: OPTIONAL `[SPEND]` — a small handful more clips (1–4), still ≤ $1 — REQUIRES EXPLICIT USER CONFIRMATION**

> Only if the user wants to see a short stretch of film. **Ask:** "Phase F Step 7 will spend roughly **$0.19–$0.76** more (1–4 additional 6s clips), keeping total spend under $1.00. Proceed?" Wait for explicit "yes."

After confirmation, repeat Step 5.2 with `--max-shots N` where `N ≤ 4`, then re-run Step 6's spend report. **Confirm the cumulative USD counter stays below $1.00 at every check.** If the guard raises `MiniMaxBudgetExceeded`, that is correct behaviour — stop.

- [ ] **Step 8: Turn the live gate OFF and restore safe defaults (NO SPEND — REQUIRED)**

1. Set `KINORA_LIVE_VIDEO=false` in `backend/.env`.
2. (Optional) revert `VIDEO_BACKEND` to `dashscope` and restore `BUDGET_CEILING_VIDEO_S`/per-session/per-scene to their prior values if this was a one-off test.
3. Restart so the gate is really off:
   Run: `docker compose -f infra/docker-compose.yml restart api render-worker`
4. Confirm the gate is off (a render now degrades to Ken-Burns, no spend):
   Run: `docker compose -f infra/docker-compose.yml logs --tail=20 render-worker | grep -iE "live_video_disabled|degrade"`
   Expected: subsequent renders show `live_video_disabled` / `degrade.shipped` (no MiniMax submit).
5. Final spend report:
   Run: `docker compose -f infra/docker-compose.yml exec redis redis-cli GET kinora:minimax:usd_spent`
   **Record the final total and confirm it matches the number of clips generated × $0.19, and is far below $30.**

- [ ] **Step 9: Verification gate (no commit)**

Confirm: live gate is OFF; total tracked spend recorded and tiny (≤ $1); at least one MiniMax clip persisted to object storage and was retrievable. Leave `backend/.env` in the desired final state. Do NOT commit `backend/.env` (it is gitignored anyway) and do NOT commit any code as part of Phase F.

---

## Self-Review (completed during authoring)

- **Spec coverage (Phase E + F only):**
  - E config (`video_backend`, `minimax_*`, `budget_ceiling_usd`) → Task 1. ✓
  - `MiniMaxVideoProvider` submit t2v + i2v → Tasks 3 (`_submit_body`) + 4 (image rules). ✓
  - poll status mapping (`Preparing|Queueing|Processing|Success|Fail`) → Task 3 `_map_status` + `_poll_to_completion`. ✓
  - retrieve → `response.file.download_url` → Task 3 `_retrieve_download_url`. ✓
  - download + persist (reuse pipeline) → Task 3 returns `clip_bytes`; persistence is the existing `_accept` path (documented in Global Constraints + Phase F Step 6). ✓
  - `LiveVideoDisabled` gating before any network call → Task 3 `render` step 1. ✓
  - `record_usage(video_seconds=duration)` → Task 3. ✓
  - USD spend guard, persisted, refuses past ceiling, persists across instances → Task 2 + Task 3 tests. ✓
  - selection branch in `create_providers()` + selection test → Task 5. ✓
  - $30→seconds budget-math test (≈947s / 157 clips) → Task 5. ✓
  - Phase F gated tiny run, spend before/after, persistence + retrievability, gate back off → Phase F Steps 1–9. ✓
- **Placeholder scan:** the only "stub" is the deliberate Task-3 passthrough for `normalize_first_frame_image`, explicitly replaced in Task 4; the `if False else` artifact is called out and fixed in Step 3b. No TBD/TODO/"handle edge cases". ✓
- **Type consistency:** `MiniMaxVideoProvider`, `SpendStore`/`get_usd`/`add_usd`, `would_exceed_usd(current_usd, cost_per_clip_usd, ceiling_usd)`, `validate_first_frame_image`/`normalize_first_frame_image`, `build_minimax_video_provider(settings, *, usage_sink)`, and the config field names are used identically across Tasks 1–6 and Phase F. `ProviderClient.request_json` gains `params=` (Task 3 Step 3a) before any caller uses it. `Providers.video` widened to the union in Task 5. ✓
