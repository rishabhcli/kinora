# Kinora overnight — published CONTRACTS

> Append-only. Each agent publishes the interfaces others may adopt. Default-off / behavior-preserving unless stated.

---

## Agent 07 — Optimization (perf helpers, cost meter, config flags)

### Backend — `app/optim/` (NEW package, additive)

**`cost_meter.py`**
- `Price` (frozen dataclass): per-model unit prices — `input_per_1k`, `output_per_1k` (tokens), `per_image`, `per_audio_second`, `per_video_second` (all USD, `Decimal`).
- `PRICING: dict[str, Price]` — table keyed by model id (Qwen/Wan). Documented "prices as of" date; override via settings.
- `cost_of(usage: Usage, pricing: Mapping[str, Price] = PRICING) -> Decimal` — pure: USD cost of one `providers.types.Usage`. Unknown model → `Decimal(0)` + structured warn (never raises in a hot path).
- `CostMeter` — implements `UsageSink` (`__call__(usage: Usage) -> None`). Rolls up `{total, by_model, by_operation, by_book, by_session}`. Attribution via `cost_context(...)`.
- `cost_context(*, book_id=None, session_id=None)` — contextmanager setting a `ContextVar` the meter reads for per-book/per-session attribution. No-op safe when unset.
- Wires via `create_providers(usage_sink=CostMeter(...))` at the `Container.providers` seam (proposed to Agent 12 — see requests).

**`routing.py`**
- `ModelRouter.route(site: str, default_model: str) -> str` — returns the cheapest model that holds the quality bar for a call-site; **default table returns `default_model` unchanged** (zero behavior change until an override is enabled). Per-site overrides + a quality guard.
- Call-site keys: `"showrunner" | "continuity" | "adapter" | "cinematographer" | "critic" | "comment_classifier"`.

**`prompt_compress.py`** — pure helpers: `estimate_tokens(text)`, `dedupe_canon(blocks)`, `trim_context(messages, budget_tokens)`, `compact_json_schema(...)`. Cut input tokens + retries; behavior-preserving.

**`batch.py`** — `gather_bounded(coros, *, limit)`, `with_backoff(fn, *, retries, on=RateQuota)` — bounded concurrency + clean `429 Throttling.RateQuota` backoff.

### Backend — new config flags (default-safe; proposed to Agent 12 for `config.py`)
- `optim_cost_meter_enabled: bool = False` — attach the `CostMeter` usage sink.
- `optim_routing_enabled: bool = False` — let `ModelRouter` overrides take effect (off ⇒ current models).
- `optim_cache_enabled: bool = False` — enable content-hash memoization of deterministic agent outputs.
- (Pricing override) `optim_pricing_json: str | None = None` — optional JSON to override `PRICING`.

### Backend — new route (additive; Agent 12 registers the include)
- `app/api/routes/optim.py` → `GET /api/optim/cost` (per-book/session rollup JSON) + `GET /api/optim/perf` (latency/queue snapshot). **Named `optim.py`, not `metrics.py`** — `metrics.py` is already the `/eval` route.

### Client — `apps/desktop/src/lib/perf.ts` (NEW, opt-in helpers)
- `lazyImport<T>(factory: () => Promise<{default: T}>): LazyExoticComponent` — `React.lazy` + retry-on-chunk-error wrapper.
- `preloadVideo(url: string, opts?): void` — prefetch + warm the HTTP cache for an upcoming clip.
- `decodeOnIdle(img: HTMLImageElement): Promise<void>` — `requestIdleCallback`-gated `img.decode()`.
- `mark(name)`, `measure(name, startMark)` — thin `performance.*` wrappers for TTI / decode marks.

_Adopt opt-in; none of these change behavior unless a component imports them._

---

<!-- Other agents: append your section below. -->
