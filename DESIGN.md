# DESIGN.md — Providers Resilience Gateway

> Living roadmap for the **provider resilience gateway** domain. All work here is
> additive to round-1's provider layer and never edits round-1 files (`audio.py`,
> `prosody.py`, `video_router.py`, `video.py`, `tts.py`, `chat.py`, `image.py`,
> `vl.py`, `embeddings.py`, `errors.py`, `types.py`).

## Scope & ownership

Owned files (free to edit/create):
- `backend/app/providers/base.py` *(round-2 owner; additive-only changes that keep
  the round-1 `ProviderClient` API intact)*
- `backend/app/providers/__init__.py` *(additive exports + opt-in wiring)*
- **New gateway/resilience modules** under `backend/app/providers/resilience/`.

Additive shared-file changes (documented at bottom): `core/config.py`. Append-only.

## Goal (task brief + kinora.md §11.1 / §12.1 / §12.3)

A hardened provider gateway around the shared `ProviderClient`:
1. **Per-model circuit breakers** with half-open probing.
2. **Adaptive token-bucket rate limiting** that backs off on 429s (AIMD).
3. **Retries with full jitter** (full / equal / decorrelated jitter schedules).
4. **Hedged / duplicate requests** for tail-latency cuts.
5. **Response cache** keyed by a stable request hash (+ in-flight dedup, §12.3).
6. **Multi-cloud provider-abstraction registry** (DashScope/OpenAI/others) with
   capability negotiation.
7. **Unified usage metering** into the budget sink (one `Usage` currency, §11.1).
8. **Fault-injection + chaos test suite.**

Hard rule overriding everything: `KINORA_LIVE_VIDEO` OFF, zero credits.
`LiveVideoDisabled` is a deliberate spend gate, never counted as a fault.

## Module map (`backend/app/providers/resilience/`)

| Module | Responsibility | Status |
|---|---|---|
| `__init__.py` | package aggregator + public re-exports | done |
| `backoff.py` | jitter schedules (full / equal / decorrelated) + Retry-After | done |
| `ratelimit.py` | `AdaptiveTokenBucket` — AIMD rate that drops on 429 | done |
| `breakers.py` | `BreakerRegistry` — per-model breakers, half-open probing | done |
| `cache.py` | `ResponseCache` — request-hash keyed TTL+LRU + in-flight dedup | done |
| `hedging.py` | `HedgedExecutor` — duplicate request tail-cut | done |
| `metering.py` | `MeteringSink` — fan-out + per-model usage rollups | done |
| `registry.py` | `ProviderRegistry` + `Capability` negotiation | done |
| `stats.py` | snapshot/inspection dataclasses for tests + telemetry | done |
| `gateway.py` | `ResilientGateway` — composes all of the above | done |
| `factory.py` | build gateway + registry from `Settings` (config translation) | done |
| `facade.py` | `GatewayChatProvider`/`GatewayCallable` — wrap round-1 providers | done |
| `degradation.py` | `DegradationAdvisor` — §11.1 budget-floor → degrade level | done |

## Phases

- **P1 — backoff schedules** (`backoff.py`). DONE
- **P2 — adaptive rate limiter** (`ratelimit.py`). DONE
- **P3 — per-model breaker registry** (`breakers.py`). DONE
- **P4 — response cache + in-flight dedup** (`cache.py`). DONE
- **P5 — hedged execution** (`hedging.py`). DONE
- **P6 — unified metering sink** (`metering.py`). DONE
- **P7 — multi-cloud registry + capability negotiation** (`registry.py`). DONE
- **P8 — gateway composition + stats** (`gateway.py`, `stats.py`). DONE
- **P9 — chaos / fault-injection harness + suite** (`chaos.py` + tests). DONE
- **P10 — config wiring (additive, opt-in)** + `factory.py`. DONE
- **P11 — depth: gateway-wrapped provider facades** (`facade.py`) — compose the
  round-1 `ChatProvider`/any async method through the gateway without editing it. DONE
- **P12 — depth: §11.1 budget-floor degradation advisor** (`degradation.py`) —
  maps metered video-seconds against a budget window to a degrade level + the
  `budget_low` UI signal, read-only (never touches the spend gate). DONE

## Key bug found + fixed during the chaos suite

`AdaptiveTokenBucket.acquire` could spin forever when a refill landed a hair below
the requested token count (float dust): the sub-epsilon `wait_s`, added to a large
clock value, rounded away to zero elapsed time. Fixed with a `_TOKEN_EPSILON`
tolerance on the availability check (and `make_async_sleep` now yields to the loop).

## Additive shared-file changes

- `core/config.py`: appended a `# --- Provider resilience gateway ---` block of
  opt-in settings (all defaulted; gateway opt-in via `provider_gateway_enabled`).

## Test results

`backend/tests/test_providers_resilience_*.py` — all green under
`backend/.venv/bin/pytest backend/tests/ -q` with no infra.
