"""Typed application settings, loaded from environment / backend/.env.

All configuration flows through :class:`Settings`. Defaults target localhost so
the app runs outside Docker; Docker Compose overrides the infra URLs to the
in-network service hostnames. The DashScope API key is the only required value.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The insecure JWT secret placeholder. Booting with this outside ``local`` is a
#: hard error (see :meth:`Settings._guard_production_secrets`).
DEFAULT_JWT_SECRET = "change-me-in-prod"  # noqa: S105 - sentinel, not a real credential
#: The insecure API-key pepper placeholder; like the JWT secret, booting with it
#: outside ``local`` is a hard error (an unkeyed API-key digest is forgeable).
DEFAULT_API_KEY_PEPPER = "change-me-api-key-pepper"  # noqa: S105 - sentinel


class Settings(BaseSettings):
    """Strongly-typed Kinora configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime ---
    app_env: str = "local"
    log_level: str = "INFO"
    service_name: str = "kinora"

    # --- DashScope / Model Studio ---
    # Required: no default so a missing key fails fast at startup.
    dashscope_api_key: str
    # The OpenAI-compatible "/compatible-mode/v1" path is appended in the providers phase.
    dashscope_base_url: str = "https://dashscope-intl.aliyuncs.com"

    # --- OpenAI (optional reasoning provider) ---
    # When ``reasoning_provider="openai"`` the agent crew's chat/reasoning calls
    # (Showrunner/Adapter/Continuity/Cinematographer + the §5.4 comment router)
    # route to OpenAI instead of DashScope; image / TTS / Wan video stay on
    # DashScope. Callers keep passing Qwen ids (e.g. ``chat_model_adapter``); the
    # OpenAI chat provider ignores them and forces ``reasoning_model``.
    # NOTE: "gpt-5.6-terra" was requested but is not available on this account
    # (verified 2026-06-28 via /v1/models); gpt-5.5 is the latest GPT-5 reasoning
    # model and is the default fallback.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    reasoning_provider: str = "dashscope"  # "dashscope" | "openai"
    reasoning_model: str = "gpt-5.5"
    reasoning_effort: str = "high"  # OpenAI reasoning_effort: minimal|low|medium|high
    # GPT-5 reasoning models bill reasoning tokens against the completion cap, so
    # the chat path needs headroom beyond the visible output (callers size
    # ``max_tokens`` for Qwen's output-only accounting). Used as a floor on
    # ``max_completion_tokens``.
    reasoning_max_output_tokens: int = 8192

    # --- Storyboard planning (app/video/storyboard) ---
    # Defaults for the model-agnostic prompt-to-storyboard planner (§9.1 step 4,
    # §9.3): the pacing target a passage's shot list is fit to, the §-style
    # shot-count ceiling, and the wan per-shot duration band. Purely additive —
    # the planner accepts an explicit StoryboardBudget per call and only falls
    # back to these. The planner's reasoning seam follows ``reasoning_provider``;
    # the default is the deterministic heuristic provider (no network).
    storyboard_target_total_s: float = 30.0
    storyboard_tolerance_s: float = 2.0
    storyboard_max_shots: int = 12
    storyboard_min_shot_s: float = 3.0
    storyboard_max_shot_s: float = 8.0

    # --- Model ids ---
    # NOTE: ids below are verified available on the DashScope-intl tier we ship
    # against (see scripts/provider_preflight.py). Some sibling ids return
    # 403 AllocationQuota.FreeTierOnly even when others in the same family work
    # — notably qwen3.5-plus (use qwen3.7-plus) and qwen-image-2.0-pro
    # (use qwen-image-plus). Do not "upgrade" these without re-running preflight.
    chat_model_max: str = "qwen3.7-max"
    chat_model_plus: str = "qwen3.7-plus"
    chat_model_adapter: str = "qwen3.7-plus"
    vl_model: str = "qwen-vl-max"
    image_model: str = "qwen-image-plus"
    image_edit_model: str = "qwen-image-edit-max"
    # Default narration TTS model. qwen3-tts-flash serves the PRESET voices that
    # ingest.identity_lock assigns (e.g. "Cherry", "Ryan"), so preset-voice
    # narration works out of the box. qwen3-tts-vc is the separate voice-CLONE
    # model (it rejects preset voice ids); clone callers use ``tts_clone_model``.
    tts_model: str = "qwen3-tts-flash"
    tts_clone_model: str = "qwen3-tts-vc"
    # Hosted DashScope Wan models only. These defaults favor reliable demo
    # latency; quality overrides are documented in .env.example / README.
    video_model: str = "wan2.1-t2v-turbo"
    video_model_i2v: str = "wan2.1-i2v-turbo"
    video_model_r2v: str = "wan2.1-i2v-turbo"

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

    # --- Frontier hosted video adapters (additive; app/video/adapters/frontier) ---
    # Concrete adapters for Runway / Luma / Pika / Kling / Veo / Sora behind one
    # UniversalVideoProvider interface. EVERY real network call is gated by BOTH the
    # global KINORA_LIVE_VIDEO spend gate AND this transport flag, which defaults OFF
    # — with it off the transport refuses to issue any HTTP request (tests inject a
    # fake transport instead). No keys are required to import/construct an adapter.
    frontier_video_enabled: bool = False
    # Per-provider bearer keys + API bases (None → that adapter is unconfigured and
    # will refuse to submit; constructing it for capability inspection is still fine).
    runway_api_key: str | None = None
    runway_base_url: str = "https://api.dev.runwayml.com/v1"
    runway_model: str = "gen4_turbo"
    luma_api_key: str | None = None
    luma_base_url: str = "https://api.lumalabs.ai/dream-machine/v1"
    luma_model: str = "ray-2"
    pika_api_key: str | None = None
    pika_base_url: str = "https://api.pika.art/v1"
    pika_model: str = "pika-2.2"
    kling_api_key: str | None = None
    kling_base_url: str = "https://api.klingai.com/v1"
    kling_model: str = "kling-v2-master"
    veo_api_key: str | None = None
    veo_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    veo_model: str = "veo-3.0-generate-preview"
    sora_api_key: str | None = None
    sora_base_url: str = "https://api.openai.com/v1"
    sora_model: str = "sora-2"
    # Shared poll bounds for the frontier async-job lifecycle.
    frontier_poll_timeout_s: float = 600.0
    frontier_poll_interval_s: float = 5.0
    frontier_poll_max_interval_s: float = 20.0
    # --- Video warm-pool / cold-start optimisation (additive; app.video.warmpool) ---
    # Per-provider pool of warm, reusable provider sessions (auth tokens + HTTP
    # connections + signed sessions) that the render path borrows/returns under a
    # fair lease, kept "just warm enough" by a cost-aware pre-warm scheduler driven
    # by predicted near-term demand (reader velocity / scheduler look-ahead). Hides
    # first-request cold-start latency so generation stays a few seconds ahead of
    # the reader. OFF by default: when off the manager opens sessions strictly on
    # demand (cold every time) and runs no keep-alive loop, so adopting the package
    # changes nothing. Manages CONNECTIONS, never renders — never touches the
    # KINORA_LIVE_VIDEO spend gate. See app/video/warmpool/DESIGN.md.
    warmpool_enabled: bool = False
    warmpool_min_warm: int = 1
    warmpool_max_size: int = 4
    warmpool_max_warm: int = 3
    warmpool_idle_ttl_s: float = 120.0
    warmpool_health_check_interval_s: float = 30.0
    warmpool_max_session_age_s: float = 600.0
    warmpool_keepalive_interval_s: float = 5.0
    warmpool_prewarm_horizon_s: float = 8.0
    warmpool_warm_worth_threshold_s: float = 0.5
    warmpool_borrow_timeout_s: float = 10.0

    # --- Wan task polling ---
    video_poll_timeout_s: float = 600.0
    video_poll_interval_s: float = 3.0
    video_poll_max_interval_s: float = 15.0

    # --- Audio backend selection (additive; provider-agnostic seam) ---
    # Which audio (narration/music/SFX) provider the universal ``app.audio`` seam
    # prefers. "dashscope" wraps the existing CosyVoice/Qwen3-TTS provider unchanged
    # (default — narration behaviour is byte-for-byte identical to today). Other
    # values ("elevenlabs" | "openai" | "azure" | "google") select a hosted adapter
    # when its key is configured; an ``AudioRouter`` may compose several with
    # health-based failover. None of this is wired into the live narration path until
    # a caller injects ``app.audio.NarrationSeam`` into the Generator.
    audio_backend: str = "dashscope"
    # Comma-separated failover order for ``app.audio.AudioRouter`` (first = preferred);
    # empty → single-backend (``audio_backend``) only. Pure config; no env reads here.
    audio_router_backends: str = ""
    # Prefer a backend that emits inline word timestamps for narration (most precise
    # karaoke map) over priority order, when one is available in the router.
    audio_prefer_inline_timestamps: bool = False
    # Per-backend circuit-breaker tunables for the audio router (mirror the video
    # router defaults).
    audio_router_failure_threshold: int = 3
    audio_router_cooldown_s: float = 30.0

    # --- Hosted audio adapter keys (optional; absent → that adapter is unavailable) ---
    elevenlabs_api_key: str | None = None
    elevenlabs_model: str = "eleven_multilingual_v2"
    openai_tts_model: str = "gpt-4o-mini-tts"  # uses OPENAI_API_KEY when present
    azure_speech_key: str | None = None
    azure_speech_region: str | None = None
    google_tts_credentials_json: str | None = None
    # --- Output normalization (app.video.normalize; additive) ---
    # Provider clips arrive in wildly different codecs/containers/fps/resolutions
    # /colour-spaces/loudness. The normalize pipeline transcodes any input to one
    # canonical, stitch-ready target so clips from ANY backend are interchangeable
    # downstream (app.render.pipeline no longer carries provider-specific shape
    # assumptions). These defaults mirror the §4.2 vertical film geometry and the
    # x264/yuv420p/AAC params the existing stitch path already enforces — flipping
    # them re-targets the whole normalization layer at once.
    normalize_target_width: int = 720
    normalize_target_height: int = 1280
    normalize_target_fps: int = 30
    # "pad" (letterbox/pillarbox — never crops content) | "crop" (fill, may crop)
    # | "stretch" (ignore aspect — distorts; discouraged) | "none" (scale only,
    # may change aspect to exactly fit). Pad is the safe default for mixed sources.
    normalize_aspect_strategy: str = "pad"
    normalize_video_codec: str = "libx264"
    normalize_pixel_format: str = "yuv420p"
    normalize_x264_preset: str = "veryfast"
    # Constant-rate factor for the canonical x264 encode (lower = higher quality).
    normalize_x264_crf: int = 20
    normalize_audio_codec: str = "aac"
    normalize_audio_bitrate: str = "128k"
    normalize_audio_sample_rate: int = 48000
    normalize_audio_channels: int = 2
    # Optional EBU R128 loudness normalisation of the audio to a target integrated
    # loudness. 0.0 disables it (the encode keeps the source loudness). A typical
    # streaming target is -16.0 LUFS; -23.0 is broadcast.
    normalize_target_lufs: float = 0.0
    normalize_loudness_true_peak: float = -1.5
    normalize_loudness_range: float = 11.0
    # Tag the canonical output as limited-range BT.709 so every downstream clip
    # carries identical colour metadata (mixed-provider clips otherwise disagree).
    normalize_color_primaries: str = "bt709"
    normalize_color_transfer: str = "bt709"
    normalize_color_space: str = "bt709"
    normalize_color_range: str = "tv"  # "tv" (limited) | "pc" (full)
    # Subprocess wall-clock ceiling for a single normalize/concat ffmpeg invocation.
    normalize_ffmpeg_timeout_s: float = 240.0

    # --- Embeddings ---
    # ``tongyi-embedding-vision-plus`` embeds BOTH images and text into one shared
    # 1152-dim space (verified live), which is exactly what CCS (image-vs-image)
    # and episodic shot retrieval need. The same model is used for text so canon
    # text and image/shot vectors live in the same space. ``embed_dim`` is the
    # canonical pgvector dimension D for entities.embedding / shots.embedding;
    # changing it requires a DB migration.
    embed_model_image: str = "tongyi-embedding-vision-plus"
    embed_model_text: str = "tongyi-embedding-vision-plus"
    embed_dim: int = 1152

    # --- Provider resilience gateway (additive; round-2) ---
    # Opt-in hardened gateway around the shared ProviderClient: per-model circuit
    # breakers (half-open probing), an AIMD adaptive token bucket that backs off on
    # 429s, full-jitter retries that honor Retry-After, hedged/duplicate requests for
    # tail-latency cuts, a request-hash response cache + in-flight dedup, and a
    # multi-cloud capability registry. OFF by default so the round-1 single-client
    # path is byte-for-byte unchanged unless flipped on. See
    # ``app.providers.resilience`` + DESIGN.md. None of these touch the KINORA_LIVE_VIDEO
    # spend gate — LiveVideoDisabled is propagated unchanged and never a breaker fault.
    provider_gateway_enabled: bool = False
    provider_gateway_max_attempts: int = 4
    provider_gateway_backoff_base_s: float = 0.5
    provider_gateway_backoff_max_s: float = 8.0
    # JitterStrategy value: "none" | "full" | "equal" | "decorrelated".
    provider_gateway_jitter: str = "full"
    provider_gateway_breaker_failure_threshold: int = 5
    provider_gateway_breaker_recovery_s: float = 20.0
    provider_gateway_breaker_half_open_max_calls: int = 1
    # Adaptive rate limiter (AIMD).
    provider_gateway_rate_initial: float = 8.0
    provider_gateway_rate_max: float = 16.0
    provider_gateway_rate_min: float = 0.5
    provider_gateway_rate_burst: int = 8
    provider_gateway_rate_cooldown_s: float = 5.0
    # Response cache.
    provider_gateway_cache_enabled: bool = True
    provider_gateway_cache_max_entries: int = 512
    provider_gateway_cache_ttl_s: float = 300.0
    # Hedging (idempotent ops only; max_attempts=1 disables it). Never hedges video.
    provider_gateway_hedge_max_attempts: int = 1
    provider_gateway_hedge_delay_s: float = 0.75

    # --- Postgres (async SQLAlchemy URL) ---
    database_url: str = "postgresql+asyncpg://kinora:kinora@localhost:5432/kinora"
    # Optional read-replica URL. When set, the DB-infrastructure read/write split
    # (``app.db.routing``) serves reads from the replica and writes from the
    # primary; unset → all traffic goes to ``database_url`` (single-node default).
    database_replica_url: str | None = None
    # Connection-pool / timeout knobs consumed by ``app.db.engine.EngineConfig``.
    # Defaults reproduce the historical hard-coded engine behaviour, so adopting
    # the typed builder changes nothing until a knob is overridden.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: float = 30.0
    db_pool_recycle_s: int = 1800
    # Server-side statement_timeout in ms (0 = unlimited) applied per connection.
    db_statement_timeout_ms: int = 0
    # Queries slower than this are logged + captured in the slow-query ring buffer.
    db_slow_query_ms: float = 500.0

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- S3 / object storage ---
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "kinora"
    s3_secret_key: str = "kinora-secret"
    s3_bucket: str = "kinora"
    s3_public_base_url: str | None = None

    # --- Scheduler watermarks / horizons (seconds of reading-time) ---
    watermark_low_s: float = 25
    watermark_high_s: float = 75
    commit_horizon_s: float = 45
    spec_horizon_s: float = 240

    # --- Budget (video-seconds) ---
    budget_ceiling_video_s: float = 1650
    budget_per_session_s: float = 300
    budget_per_scene_s: float = 90
    budget_low_floor_s: float = 120
    # Hard USD ceiling for the MiniMax provider's belt-and-suspenders spend guard
    # (kinora.md §11.1). The primary cap is still the video-seconds ledger; this
    # is a second, independent refusal that protects against duration/config drift.
    budget_ceiling_usd: float = 30.0

    # --- FinOps / cost governance (additive; see app/finops, kinora.md §11.1) ---
    # A separate multi-tenant allocation of the global video-seconds ceiling.
    # <= 0 means "no separate tenant cap" (single-tenant local/demo: the global
    # ceiling is the only ceiling).
    finops_tenant_ceiling_video_s: float = 0.0
    # Tiered-alert fractions of any cap (info < warning < soft <= 1.0). The soft
    # cap is the "prefer-degrade / warn loudly" line below the hard cap.
    finops_alert_info_fraction: float = 0.50
    finops_alert_warning_fraction: float = 0.75
    finops_soft_cap_fraction: float = 0.90
    # Forecast horizon: how far ahead (reading-seconds) the burn-down projects.
    finops_forecast_horizon_s: float = 600.0
    # Optimizer: minimum acceptable quality rung when budget-constrained (0..1).
    finops_optimizer_min_quality: float = 0.0

    # --- Tenancy / multi-tenant isolation (additive; app.tenancy, §6/§11.1) ---
    # Default per-tenant quota envelope for a freshly-created org. ``0`` == no
    # tighter-than-global cap (single-tenant local/demo: the global ceiling is the
    # only ceiling). These compose with the global video-seconds ceiling — the
    # binding cap is always the smaller of (tenant envelope, global head-room).
    tenancy_default_max_books: int = 0
    tenancy_default_monthly_usd: float = 0.0
    tenancy_default_monthly_video_seconds: float = 0.0
    # Object-store namespace segment prefixing every tenant's assets (prefix
    # isolation). Keeps a tenant's media partitioned under ``<segment>/<tenant>/…``.
    tenancy_asset_namespace: str = "t"
    # --- Speculative pre-generation engine (additive; app/video/speculate) ---
    # An expected-value policy over the ahead-of-reader buffer (kinora.md §4.4/
    # §4.6/§4.8): predict which upcoming shots the reader will reach, then
    # pre-render the EV-optimal portfolio under a *separate* speculative spend cap
    # — distinct from, and bounded under, the §11 video-seconds budget. Pure
    # policy: gates nothing on its own (KINORA_LIVE_VIDEO stays the live gate).
    speculate_enabled: bool = False
    #: Hard speculative spend cap per reading session (dollars). The portfolio
    #: optimiser never selects beyond this; cancellations refund into it.
    speculate_budget_usd: float = 1.0
    #: Forward window (words) a speculation must still reach to survive a §4.8
    #: trajectory invalidation (else it is cancelled + refunded if unstarted).
    speculate_keep_horizon_words: int = 4000
    #: Minimum expected-value-per-dollar a candidate must clear to be eligible
    #: (its expected hit value must beat its expected waste).
    speculate_min_ev_per_dollar: float = 1.0
    #: Hit-probability thresholds for probability→model routing: at/above premium
    #: earns a premium id; at/above standard earns a standard id; below → cheap.
    speculate_premium_probability: float = 0.7
    speculate_standard_probability: float = 0.35

    # --- Live video go-live gate ---
    kinora_live_video: bool = False

    # --- Unified VideoGenerationService facade (app/video/service; ADDITIVE) ---
    #: Bounded provider attempts inside the facade before it falls through to a
    #: SKIP the render pipeline degrades. A quality-gate reject re-rolls the seed
    #: and retries up to this cap; >1 only matters when a quality gate is wired.
    video_service_max_attempts: int = 3
    #: Per-job await deadline (seconds) the facade passes to the job lifecycle;
    #: ``None`` defers to the provider's own ``video_poll_timeout_s``.
    video_service_job_timeout_s: float | None = None
    # --- Shadow / live-eval harness (app/video/shadow; ADDITIVE, off-by-default) ---
    # Evaluate a candidate video model against real workloads OFF the critical path
    # before promoting it to reader traffic. Every default is the safe one: shadow
    # mode is opt-in, nothing is sampled until a fraction is set, and the candidate
    # eval budget is UNFUNDED (0.0) so enabling shadow mode can never spend a real
    # video-second or touch the reader budget (independent of KINORA_LIVE_VIDEO).
    video_shadow_enabled: bool = False
    video_shadow_sample_fraction: float = 0.0
    video_shadow_sample_salt: str = "shadow"
    video_shadow_eval_video_seconds: float = 0.0
    video_shadow_candidate_model: str = ""
    video_shadow_confidence: float = 0.95
    video_shadow_win_margin: float = 0.0
    # --- Multi-model best-of-N ensemble (app/video/ensemble; §9.5/§11) ---
    # Render a hero shot on K models and keep the best. OFF by default and capped
    # at one candidate so an accidental wiring can NEVER fan out or overspend; a
    # caller builds an EnsembleConfig from these into the BestOfNRenderer.
    ensemble_enabled: bool = False
    # Comma-separated shot tiers permitted to fan out (e.g. "hero"). Empty → none.
    ensemble_enabled_tiers: str = ""
    # Max providers launched per shot (1 → no fan-out even when enabled).
    ensemble_max_candidates: int = 1
    # Max providers rendering concurrently (bounds peak fan-out / cost spike).
    ensemble_max_concurrency: int = 2
    # Selection objective: max_quality | quality_per_cost | quality_under_cost_cap
    # | consistency_vote.
    ensemble_objective: str = "max_quality"
    # Cost figure for value/cap objectives: video_seconds | usd.
    ensemble_cost_unit: str = "video_seconds"
    # Hard per-shot multi-render cost cap (in cost_unit; 0 → no cap).
    ensemble_per_shot_cost_cap: float = 0.0
    # Early-stop: stop launching + cancel losers once a candidate clears this
    # composite quality (0 or >1 → never early-stop).
    ensemble_good_enough_quality: float = 0.0

    # --- Concurrency lanes ---
    concurrency_committed: int = 4
    concurrency_speculative: int = 2
    concurrency_keyframe: int = 2
    retry_cap: int = 2

    # --- Resilience chaos harness (app/resilience; ADDITIVE) ---
    # Probabilistic fault/latency injection for proving the resilience policies.
    # Hard-gated: app/resilience/chaos.chaos_from_settings refuses to ARM this
    # outside the ``local`` environment even when the flag is True, so it can
    # never inject faults into a real (non-local) deployment.
    resilience_chaos_enabled: bool = False
    resilience_chaos_fault_probability: float = 0.0
    resilience_chaos_latency_probability: float = 0.0
    resilience_chaos_latency_min_s: float = 0.0
    resilience_chaos_latency_max_s: float = 0.0

    # --- Render-engine hardening (§9.7 resumability/poison; ADDITIVE) ---
    #: Snapshot in-flight shots so a worker restart resumes mid-render instead of
    #: re-spending video-seconds. Safe default ON (the in-memory store is a no-op
    #: across processes until a real CheckpointStore adapter is wired).
    render_checkpoint_enabled: bool = True
    #: Hard render crashes a shot may accrue before it is quarantined to the
    #: bottom rung (poison handling) so one pathological shot can't wedge a lane.
    render_poison_threshold: int = 3
    #: Max shots a DAG batch releases in parallel (caps render fan-out).
    render_max_parallel_shots: int = 4
    #: Cadence + batch size for the durable stuck-shot recovery loop
    #: (``python -m app.render.durability.recovery``): finds shots left in a
    #: non-terminal §9.7 state after a worker restart and resumes/repairs them.
    render_recovery_interval_s: float = 30.0
    render_recovery_limit: int = 50

    # --- Video provider quality scoring / auto-eval (app/video/quality; ADDITIVE) ---
    #: Use the real VL scorer for the perceptual axes (aesthetic / prompt-adherence /
    #: NSFW) in the quality harness. OFF by default so scoring never spends — the
    #: evaluator falls back to a neutral fake. This makes a VL *chat* call (not a
    #: video call), independent of ``kinora_live_video``; flip on only when intended.
    video_quality_vl_enabled: bool = False
    #: Per-provider reputation EWMA half-life, in *observations* (clips). The last
    #: ~``half_life`` clips dominate a provider's rolling quality reputation.
    video_quality_ledger_half_life: float = 20.0
    #: Max frames the VL scorer samples per clip (caps tokens on the perceptual call).
    video_quality_vl_max_frames: int = 4

    # --- Media transform graph (app/video/mediagraph; derived-media DAG; ADDITIVE) ---
    #: Max independent media-transform nodes a wave runs concurrently (thumbnail /
    #: poster / gif / sprite off one master fan out up to this width).
    mediagraph_max_parallel: int = 4
    #: Skip already-produced derivatives via per-node content-hash caching, so an
    #: idempotent re-run does no ffmpeg work. OFF disables caching (every node runs).
    mediagraph_cache_enabled: bool = True
    #: Per-ffmpeg-invocation wall-clock ceiling (seconds) for the media graph.
    mediagraph_ffmpeg_timeout_s: float = 240.0

    # --- Render-queue retry backoff (kinora.md §12.1) ---
    # Jitter strategy for the exponential-backoff retry schedule
    # (none|full|equal|decorrelated). ``none`` keeps the literal fixed schedule
    # below (back-compat); the others spread retries to avoid a thundering herd.
    queue_backoff_jitter: str = "none"
    queue_backoff_base_s: float = 2.0
    queue_backoff_cap_s: float = 30.0
    # The literal jitter-free schedule used when queue_backoff_jitter == "none".
    queue_retry_backoff_s: list[float] = [2.0, 8.0, 30.0]

    # --- Render-queue admission / fairness (kinora.md §12.2) ---
    # Total queued depth past which *new speculative* enqueues are shed.
    queue_backpressure_depth: int = 64
    # Max concurrent renders one session may hold (per-session fairness); 0 = off.
    queue_session_render_cap: int = 0

    # --- Render-worker autoscaling (kinora.md §4.9/§12.2) ---
    # Upper bounds for the elastic lane pools; the §4.9 caps are the lower bounds.
    queue_autoscale_committed_max: int = 8
    queue_autoscale_speculative_max: int = 4
    queue_autoscale_cooldown_s: float = 30.0

    # --- Adaptive scheduler v2 (kinora.md §4.5/§4.6/§4.9; app/scheduler/v2) ---
    # An ADDITIVE strategy layer over the dual-watermark buffer. OFF by default so
    # the live scheduler behaves byte-for-byte as today; flip on to let the
    # velocity-regime model resize watermarks, the provider-aware policy fan
    # promotions across free render slots, and the cold-zone prefetch/eviction
    # decide what to pre-warm. Spends NO extra video-seconds the budget gate would
    # not already allow — every promotion stays ``budget.can_render_live()``-gated.
    scheduler_v2_enabled: bool = False
    # Reader-velocity regime classifier (§4.6): a skim must exceed this multiple of
    # the §4.3 clamp ceiling to read as SKIMMING; a re-read needs this fraction of
    # recent motion to be backward; the dwell (ms) above which a steady reader is
    # reclassified as PONDERING (long thinks).
    scheduler_v2_skim_ceiling_multiple: float = 1.0
    scheduler_v2_reread_backward_fraction: float = 0.35
    scheduler_v2_ponder_dwell_ms: float = 6_000.0
    # How many samples the regime classifier needs before it leaves the cold-start
    # STEADY default (small counts stay conservative — no spend regression).
    scheduler_v2_regime_min_samples: int = 3
    # Provider-aware promotion (§4.9): per-provider nominal render latency (seconds)
    # used to estimate how many in-flight slots will free before the buffer drains,
    # so the policy promotes enough — but not more — to keep the buffer full.
    scheduler_v2_provider_latency_s: float = 12.0
    # Hard ceiling on parallel promotions one tick may release (caps fan-out even
    # when many slots look free); 0 falls back to the committed lane width.
    scheduler_v2_max_parallel_promotions: int = 0
    # Cold-zone prefetch/eviction (§4.4): keyframes to keep warm past the
    # speculative horizon, and the eviction high-watermark for the warm cache.
    scheduler_v2_prefetch_depth: int = 4
    scheduler_v2_cold_cache_capacity: int = 24
    # --- Distributed render orchestration (kinora.md §12.1/§12.2; ADDITIVE) ---
    # The coordination layer beside the single render queue: a worker registry +
    # lease model, capability/locality-aware assignment, and work-stealing across
    # many workers/providers (app/orchestration/). All knobs are ms / counts.
    #: A worker silent longer than this is DEAD; its leases are reclaimed + reassigned.
    orchestration_worker_ttl_ms: int = 90_000
    #: Lease window granted on assignment — must outlast a render (mirrors §12.1).
    orchestration_lease_ttl_ms: int = 120_000
    #: Min lease-count gap between busiest and idlest worker before work-stealing fires.
    orchestration_rebalance_imbalance: int = 2
    #: Max shot migrations one rebalance pass may plan (anti-thrash).
    orchestration_rebalance_max_steals: int = 4
    #: Whether committed shots may be stolen (default off: keep them sticky for continuity).
    orchestration_steal_committed: bool = False
    # --- Demand-aware lane autoscaler (additive; see app/autoscale, §4.6/§4.9/§12.2) ---
    # Per-lane replica bounds for the target-tracking + predictive controller. CPU
    # is the cheap Ken-Burns lane; PROVIDER is the quota-bounded Wan/MiniMax lane;
    # GPU is the scarce local accelerated lane (defaults to 0 = off).
    autoscale_cpu_min: int = 2
    autoscale_cpu_max: int = 24
    autoscale_provider_min: int = 4
    autoscale_provider_max: int = 16
    autoscale_gpu_min: int = 0
    autoscale_gpu_max: int = 4
    # Anti-flap: scale-out is immediate; scale-in waits this cooldown. Hysteresis
    # is the scale-out dead-band (fraction of current size) below which jitter is
    # ignored; the floor is an absolute minimum scale-out margin (replicas) so a
    # one-job wobble near the floor can't flap a small pool.
    autoscale_scale_in_cooldown_s: float = 60.0
    autoscale_hysteresis_band: float = 0.15
    autoscale_hysteresis_floor: float = 1.0
    # Predictive pre-warm gain: replicas added to the committed lane per unit of
    # aggregate buffer-underrun risk (reader-velocity-driven). 0 disables pre-warm.
    autoscale_predictive_gain: float = 1.0
    # Cost-aware cap on the summed plan (relative cost_per_replica units); inf = off.
    autoscale_max_cost: float = float("inf")
    # p95 render-latency SLO (s) at/above which a lane is treated as saturated, and
    # the look-ahead horizon (s) the predictive term provisions buffer for.
    autoscale_latency_slo_s: float = 25.0
    autoscale_underrun_horizon_s: float = 30.0

    # --- Ingest recovery ---
    ingest_recovery_interval_s: float = 30.0
    ingest_recovery_limit: int = 25

    # --- Durable saga / workflow engine (app/sagas/) [additive] ---
    #: How long a worker's lease on an in-flight run is valid before the
    #: recovery sweep treats it as abandoned and re-claims it (seconds).
    saga_lease_ttl_s: float = 300.0
    #: Interval the recovery sweep runs at (fires due timers + recovers stuck
    #: runs). Read by a production scheduler; the engine itself takes no time.
    saga_recovery_interval_s: float = 15.0
    #: Max runs processed per sweep pass (back-pressure for the recovery loop).
    saga_recovery_batch: int = 100

    # --- Ingest pipeline (Phase A) [Agent: ingest-domain, additive] ---
    #: OCR fallback for scanned/image-only pages (§9.1). Off by default — it spends
    #: VL tokens, so a born-digital book never pays for it; flips on for scanned
    #: catalogues. The scanned-page heuristic still gates per-page even when on.
    ingest_ocr_enabled: bool = False
    #: Page word-count floor below which a page is OCR-candidate (image-only).
    ingest_ocr_word_floor: int = 12
    #: Max tokens for one OCR page transcription.
    ingest_ocr_max_tokens: int = 2048
    #: Multi-column / reading-order re-threading of extracted words (§9.1). On by
    #: default — it is a pure, cheap transform and a no-op for single-column pages.
    ingest_layout_reorder: bool = True
    #: Durable checkpointed milestones so a crashed ingest resumes instead of
    #: recomputing completed stages. On by default; degrades gracefully if the
    #: checkpoint table is absent (treated as "no checkpoint").
    ingest_checkpoints_enabled: bool = True
    #: Token-bucket rate limit for the per-page VL analyse calls (requests/sec).
    #: 0 disables the limiter (pure semaphore concurrency only).
    ingest_analyze_rate_per_s: float = 0.0
    #: Burst size for the analyse token bucket (max calls that can fire at once).
    ingest_analyze_rate_burst: int = 8
    #: Per-page analyse retry attempts on a transient (e.g. 429) provider error.
    ingest_analyze_max_attempts: int = 3
    #: Base backoff (seconds) for the analyse retry; grows exponentially + jitter.
    ingest_analyze_backoff_base_s: float = 1.0
    #: Retry attempts for the sequential per-page Adapter (``analyze_page``) call
    #: in shot planning — unlike the VL analyse fan-out this runs one page at a
    #: time with no concurrency to smooth, but still needs backoff so a single
    #: transient error doesn't abort the whole ingest run.
    ingest_shotplan_max_attempts: int = 3
    #: Base backoff (seconds) for the shot-plan per-page retry.
    ingest_shotplan_backoff_base_s: float = 1.0
    #: Token-bucket rate limit for identity-lock's per-character/pose image-gen
    #: calls (requests/sec). 0 disables the limiter. A book with many principal
    #: characters otherwise fires these back-to-back with no backoff.
    ingest_identity_rate_per_s: float = 0.0
    #: Burst size for the identity-lock image-gen token bucket.
    ingest_identity_rate_burst: int = 4
    #: Per-call retry attempts on a transient provider error during identity lock.
    ingest_identity_max_attempts: int = 3
    #: Base backoff (seconds) for the identity-lock retry.
    ingest_identity_backoff_base_s: float = 1.0

    # --- Search & indexing service (app/search) ---
    # The server-side corpus search engine (kinora.md §8 — search complements the
    # canon). ``search_backend`` selects the pluggable index: ``postgres`` (FTS +
    # pgvector hybrid over ``search_documents``) or ``memory`` (the in-memory
    # engine, for zero-infra / offline). ``search_alias`` is the stable alias a
    # bulk reindex atomically swaps; ``search_default_version`` is used before an
    # alias has been set. Hybrid fusion + arm weights are RRF defaults.
    search_backend: str = "postgres"
    search_alias: str = "kinora_current"
    search_default_version: str = "v1"
    search_rrf_k: int = 60
    search_lexical_weight: float = 1.0
    search_semantic_weight: float = 1.0
    search_default_limit: int = 20
    search_max_limit: int = 100

    # --- Recommendations engine (server-side recsys, app.recommendations) ---
    # Blend weights over the recsys signals (content / collaborative / taste /
    # popularity prior); the candidate-gen → score → re-rank pipeline reads these.
    # Defaults favour personalization with a small popularity prior. All knobs are
    # additive and optional — the engine falls back to its own defaults if unset.
    recs_weight_content: float = 1.0
    recs_weight_collaborative: float = 1.0
    recs_weight_taste: float = 1.2
    recs_weight_popularity: float = 0.3
    #: Candidates each recall source proposes before scoring.
    recs_candidates_per_source: int = 50
    #: Default recommendation list length after re-ranking.
    recs_top_k: int = 20
    #: MMR diversity trade-off (1.0 = pure relevance, lower = more diverse).
    recs_mmr_lambda: float = 0.75
    #: Recency half-life for the taste-vector decay, in days.
    recs_taste_half_life_days: float = 30.0
    #: Nearest neighbours the CF models consider.
    recs_cf_neighbors: int = 40
    #: Minimum shared-reader co-occurrence before an item-item CF edge is trusted.
    recs_cf_min_cooccur: int = 1
    #: Popularity damping (larger = flatter prior, big hits don't dominate).
    recs_popularity_damping: float = 10.0

    # --- Reliability / load-test / SLO (app.reliability + loadtest) ---
    # Defaults consumed by the reliability toolkit (load runner, canaries, SLO
    # gating); the CLI overrides them per-run. Additive-only — nothing above is
    # changed. See loadtest/DESIGN.md.
    load_default_users: int = 16
    load_default_duration_s: float = 60.0
    load_default_target_rps: float = 0.0  # 0 => closed model (think-time paced)
    load_ramp_seconds: float = 5.0
    #: §4.9 control-tick latency budget — intent must stay snappy so the buffer
    #: keeps up; the load report / canary gate the intent endpoint against this.
    slo_intent_p99_ms: float = 250.0
    #: §4.8 latency-to-first-frame — a seek must bridge ~instantly.
    slo_seek_coherent_p99_ms: float = 150.0
    #: The §13 availability target the load report's SLO set gates on.
    slo_availability_target: float = 0.995
    #: Default seed for the chaos engine + deterministic load runs.
    chaos_default_seed: int = 1337

    # --- Observability plane (app/observability/; §12.5) ---
    #: Mount the flag-gated Prometheus exposition router built by the
    #: observability plane. The plane is additive: the fixed ``/metrics`` route in
    #: ``app/main.py`` is unaffected; this gates only the plane's own router (e.g. a
    #: packaged build that must not expose an unauthenticated scrape can set False).
    observability_metrics_enabled: bool = True
    #: Retain finished spans in a bounded in-memory ring so the observability plane
    #: can reconstruct a shot's render-trace timeline for debugging. Off by default
    #: (the no-op exporter) so a normal run keeps zero span overhead.
    observability_collect_spans: bool = False
    # --- Deep health + SLO / error-budget engine (app.slo; additive) ---
    # The *running-service* SLO plane (distinct from the load-test SLO set above,
    # which gates a finished LoadReport). These tune the default product
    # objectives the engine tracks continuously off the live metric streams; the
    # error-budget + multi-window burn alerts + release gate derive from them.
    # Additive-only — reuses the existing slo_intent_p99_ms / slo_availability_target.
    #: Target fraction of page reads served without a buffer underrun (the §4
    #: core promise: the next page's film is ready before the reader arrives).
    slo_read_underrun_free_target: float = 0.99
    #: Target fraction of shot renders that reach an accepted asset (§9.7) — not
    #: dead-lettered or dropped to a lower ladder rung.
    slo_shot_success_target: float = 0.98
    #: Render p95 budget (ms): the buffer-fill latency a shot must clear.
    slo_render_p95_ms: float = 8000.0
    #: Window (s) the *current* SLI snapshot is computed over (the dashboard read).
    slo_eval_window_s: float = 300.0

    # --- Product analytics (app/analytics/) ---
    # Distinct from ops-observability (Prometheus) and the §13 eval warehouse:
    # the event pipeline that answers "how do humans use the product?".
    analytics_enabled: bool = True
    # Gap-based sessionization: events more than this far apart start a new
    # reading session (seconds). 30 minutes is the web-analytics standard.
    analytics_session_gap_s: float = 1800.0
    # Hard cap on events accepted in a single ingest batch.
    analytics_max_batch: int = 500
    # Days of raw events to retain before a (future) prune job drops them.
    analytics_retention_days: int = 365
    # Salt for the deterministic identifier pseudonymisation (HMAC). Defaults to
    # the JWT secret so a fresh deployment still anonymises; override to rotate
    # anon ids independently of auth.
    analytics_salt: str | None = None
    # Rollup worker cadence (seconds) and the look-back window it re-aggregates
    # each tick (re-aggregating a trailing window keeps late-arriving events
    # correct; idempotent upserts make the overlap harmless).
    analytics_rollup_interval_s: float = 300.0
    analytics_rollup_window_days: int = 2

    # --- Cost & usage analytics + dashboards (app/usageanalytics/, §11.1) ---
    # A time-series analytics warehouse over *spend* (cost/usage/quality rolled up
    # by provider/model/book/session/time-bucket), distinct from app/analytics
    # (product behaviour), app/finops (budget governance), and the cost_meter
    # (one process-global rollup). Read-only dashboard API; never spends.
    usage_analytics_enabled: bool = True
    # Monthly USD cap the burndown projects month-end spend against (the
    # "$30-style" demo cap). Read as a Decimal string to avoid float drift.
    ua_monthly_cap_usd: float = 30.0
    # Default / hard-max dashboard query window (days back from ``until``).
    ua_default_window_days: int = 30
    ua_max_window_days: int = 730
    # Trailing window (days) the burndown averages the $/day run-rate over.
    ua_run_rate_window_days: int = 7
    # Anomaly detector thresholds (overridable). Spend spike: latest bucket cost
    # must exceed baseline_mean × this ratio. Error surge: absolute error-rate
    # rise over baseline. Quality regression: absolute mean-quality drop.
    ua_spend_spike_ratio: float = 3.0
    ua_error_surge_delta: float = 0.10
    ua_quality_drop_delta: float = 0.08

    # --- Realtime transport (SSE/WS resume, presence, recorder; §5.6) ---
    # The event recorder tees every §5.6 event into a per-session log so a dropped
    # SSE/WS connection can resume from a Last-Event-ID. With a single API replica
    # (the dev/demo posture) exactly one recorder runs and ids stay clean; under
    # horizontal scale set this true so only the recorder holding a Redis lease
    # records (the others stand by), keeping ids globally consistent.
    realtime_recorder_elect_leader: bool = False

    # --- Media / asset service (app.media; additive — Media domain) ---
    #: Default signed-URL lifetime for media links (clamped 60s..7d at use).
    media_url_ttl_s: int = 3600
    #: Target HLS/DASH segment duration (seconds) when packaging films.
    media_segment_s: int = 4
    #: Number of sprite-sheet tiles generated for the scrubber preview.
    media_sprite_count: int = 20
    #: Retention horizon (days) applied to *derived* assets (poster/sprite/HLS);
    #: ``0`` disables auto-expiry. Primary assets (clips/source) are never
    #: auto-collected — their removal is governed by explicit retention only.
    media_derived_retention_days: int = 30
    #: Max rows the lifecycle GC sweep collects per run.
    media_gc_batch: int = 100

    # --- Multi-region asset CDN / replication (app.cdn; additive) ---
    #: Comma-separated replica region ids to mirror origin into (e.g. "eu,ap").
    #: Empty == single-region (origin only); the manager simply has no replicas.
    cdn_replica_regions: str = ""
    #: A replica more than this many seconds behind origin is treated as stale and
    #: skipped by the resolver in favour of a fresher region (or origin).
    cdn_max_replica_lag_s: float = 60.0
    #: Edge-cache TTL (seconds) for *immutable* content-addressed assets — set
    #: effectively "forever" so edges never revalidate a by-hash blob.
    cdn_immutable_ttl_s: int = 365 * 24 * 3600
    #: Edge-cache TTL (seconds) for *mutable* path-keyed assets (a shot that may
    #: be surgically re-rendered); a re-render is picked up within this window.
    cdn_mutable_ttl_s: int = 6 * 3600
    #: Default ceiling on how many upcoming shots the prefetch controller warms
    #: into the reader's nearest region per pass.
    cdn_prefetch_max_warm: int = 4

    # --- Event sourcing / event store (app.eventsourcing.store; additive) ---
    #: Events between aggregate snapshots (the SnapshotStrategy cadence). The
    #: domain facet consults this to keep rehydration O(events-since-snapshot).
    es_snapshot_every: int = 50
    #: Rows the transactional-outbox relay claims per drain pass (§12.1).
    es_outbox_batch: int = 100
    #: Publish attempts before an outbox row is dead-lettered (the §12.1 DLQ).
    es_outbox_max_attempts: int = 8

    # --- Tamper-evident audit log + provenance (app.audit; additive) ------- #
    # The hash-chained, redaction-aware account of every consequential action
    # (canon mutation, arbitration decision, render accept/degrade, budget spend,
    # auth/lockout, config/flag change). All additive with safe defaults so the
    # subsystem is inert until a call site records to it.
    #: Master on/off switch — when false, the AuditService is not wired into the
    #: composition root (call sites no-op). The log itself always works in-process.
    audit_enabled: bool = True
    #: Entries per sealed Merkle checkpoint segment (the auto-seal cadence). A
    #: checkpoint is a compact, publishable tamper-evidence commitment.
    audit_segment_size: int = 1024
    #: Retention horizon (days) for *sealed* entries before the retention sweep may
    #: prune them (their Merkle checkpoint still proves they existed); ``0`` keeps
    #: everything forever.
    audit_retention_days: int = 0
    #: Salt keying the PII redaction commitments (unforgeable without it,
    #: reproducible by the operator who holds it). Defaults to the JWT secret so a
    #: fresh deployment still anonymises; override to rotate independently.
    audit_redaction_salt: str | None = None
    # --- Privacy / GDPR right-to-erasure (app.privacy; additive) ---
    #: Per-data-class retention TTLs (days) for the privacy retention engine.
    #: ``0`` means "expire immediately"; a positive value sets the rolling
    #: window; leave unset to use the data-class default (None == account life).
    #: These mirror the data-map's retention_class names (datamap.RC_*).
    privacy_retention_reading_session_days: int = 365
    privacy_retention_directing_preference_days: int = 730
    #: Audit / event-stream accountability retention (~7y) for crypto-erased data.
    privacy_retention_audit_log_days: int = 2555
    privacy_retention_event_stream_days: int = 2555
    #: Marker substituted into a redacted append-only entry's personal fields.
    privacy_redaction_marker: str = "[REDACTED]"
    #: Max store-steps an erasure run drains per resume pass (idempotent/resumable).
    privacy_erasure_step_batch: int = 50

    # --- Auth (JWT) ---
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_alg: str = "HS256"
    access_token_ttl_s: int = 86400

    # --- Auth & security plane (app.auth / app.core.security) -------------- #
    # All additive with safe defaults so the gateway boots unchanged; the
    # production hardening (lockout, MFA, RBAC, API keys, refresh rotation,
    # CSRF) layers on top of the existing Bearer flow (kinora.md §6/§12).
    #
    # Issuer/audience stamped onto access tokens so a token minted for one
    # deployment cannot be replayed against another.
    jwt_issuer: str = "kinora"
    jwt_audience: str = "kinora-api"
    # Refresh tokens: long-lived opaque secrets rotated on every use with a
    # per-family reuse-detection scheme (a replayed token revokes the family).
    refresh_token_ttl_s: int = 60 * 60 * 24 * 30  # 30 days
    # Short-lived bearer that proves a partial (password-only) login while the
    # client completes the MFA challenge.
    mfa_challenge_ttl_s: int = 300
    # Password hashing: "bcrypt" (always available) or "argon2" (if installed).
    password_hasher: str = "bcrypt"
    bcrypt_rounds: int = 12
    # Password strength policy. Defaults mirror the existing RegisterRequest
    # contract (length only) so the established register/login flow is unchanged;
    # tighten these in production (charset + common-password denylist) without a
    # code change. ``app.core.security.PasswordPolicy`` implements the checks.
    password_min_length: int = 8
    password_require_upper: bool = False
    password_require_lower: bool = False
    password_require_digit: bool = False
    password_require_symbol: bool = False
    password_block_common: bool = False
    # Login throttling / account lockout.
    login_max_failures: int = 5
    login_lockout_window_s: int = 900  # failures counted within this window
    login_lockout_duration_s: int = 900  # how long a locked account stays locked
    # Per-IP login attempt backstop (a coarse sliding-window lockout, separate
    # from and looser than the gateway's ``auth_rate_limit`` token bucket which is
    # the primary fast defence). Kept above the auth bucket's capacity so the
    # bucket's typed 429 ``rate_limited`` is what a burst hits first; this layer
    # catches slow distributed credential-stuffing across the longer window.
    login_ip_max_attempts: int = 50
    login_ip_window_s: int = 300
    # MFA / TOTP.
    mfa_issuer: str = "Kinora"
    totp_drift_window: int = 1  # ±N 30s steps accepted (clock-skew tolerance)
    recovery_code_count: int = 10
    # API keys — a server-side pepper keys the HMAC used to fingerprint secrets,
    # so a leaked api_keys table is useless without it. Outside ``local`` a real
    # value is mandatory (guarded below).
    api_key_pepper: str = DEFAULT_API_KEY_PEPPER
    api_key_default_ttl_s: int | None = None  # None == non-expiring
    # Sessions — cap concurrent active sessions per user (oldest evicted).
    max_sessions_per_user: int = 25
    # CSRF — double-submit cookie protection for browser/cookie auth flows.
    csrf_enabled: bool = True
    csrf_cookie_name: str = "kinora_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    # Auth audit log retention sweep (days; 0 disables pruning).
    auth_audit_retention_days: int = 365

    # --- API hardening (app/apihardening/) — all additive, all opt-in ----------
    # Master switch for the cross-cutting hardening middleware (request-id,
    # body/content-type limits, the token-bucket rate limiter, idempotency-key
    # replay, OpenAPI docs). OFF by default so existing behaviour is byte-for-byte
    # unchanged until a deployment opts in.
    hardening_enabled: bool = False
    # Per-concern toggles (only consulted when ``hardening_enabled`` is true).
    hardening_request_id: bool = True
    hardening_request_limits: bool = True
    hardening_rate_limit: bool = True
    hardening_idempotency: bool = True
    hardening_openapi: bool = True
    # When true the hardening error surface (rate-limit 429, body-too-large 413,
    # idempotency conflicts) renders RFC-7807 ``application/problem+json`` and the
    # problem exception handlers are installed. OFF keeps the legacy
    # ``{"error": {...}}`` envelope the desktop renderer already parses.
    hardening_problem_json_enabled: bool = False
    hardening_problem_type_base: str = "https://kinora.dev/problems/"
    hardening_request_id_header: str = "X-Request-ID"
    hardening_correlation_id_header: str = "X-Correlation-ID"
    hardening_trust_inbound_request_id: bool = True
    # Max accepted request body in bytes (0 disables). 8 MiB default covers the
    # JSON/control surface; the PDF-upload route is exempted at wiring time.
    hardening_max_body_bytes: int = 8 * 1024 * 1024
    # Idempotency-Key replay window (seconds) + header name.
    hardening_idempotency_ttl_s: int = 24 * 3600
    hardening_idempotency_header: str = "Idempotency-Key"
    # Default token-bucket rate limit (burst capacity + refill tokens/sec).
    hardening_rate_limit_enabled: bool = True
    hardening_rate_limit_capacity: int = 120
    hardening_rate_limit_refill_per_s: float = 2.0

    # --- MCP control surface (the deployed canon-memory server, §8.3/§14) ---
    # When set, the streamable-HTTP MCP requires ``Authorization: Bearer <token>``.
    # Outside ``local`` it is mandatory: the HTTP MCP refuses to start without it
    # (an unauthenticated control surface must never run in prod, §12).
    mcp_auth_token: str | None = None

    # --- MCP protocol layer (the spec-compliant server around the §8.3 tools) ---
    # Tool versioning: when on, the server honours a per-call ``_meta`` version pin
    # and rejects an incompatible served version (forward-compatible API evolution).
    mcp_versioning_enabled: bool = True
    # Response validation: gate the server-contract check of each tool result
    # against its declared output JSON Schema (off => skip on the hot path in prod).
    mcp_validate_responses: bool = True
    # Resource subscriptions: advertise + serve ``resources/updated`` change
    # notifications (the inspectable, live canon, §8). When on, the streamable-HTTP
    # transport runs stateful so per-session subscriptions work end-to-end.
    mcp_resource_subscriptions: bool = True
    # Per-client scoping (§12): a JSON map of bearer-token -> grant, e.g.
    # ``{"tok_ro": {"subject": "judge", "scopes": ["read"]},
    #    "tok_full": {"scopes": ["read","write","render"], "books": ["book_1"]}}``.
    # Empty => the single shared ``mcp_auth_token`` (+ open scope locally) is the
    # only control; non-empty => unrecognised tokens are denied and each token is
    # confined to its scopes/books.
    mcp_client_scopes: dict[str, dict[str, object]] = {}

    # --- Feature flags & experimentation (app.flags) ---
    # The flag platform's pure evaluator needs none of these; they only tune the
    # service layer (cache freshness + bucketing namespace + the Redis stream
    # channel). Defaults preserve current behavior (no flags defined → every gate
    # falls through to its caller-provided default).
    flags_enabled: bool = True
    flags_cache_ttl_s: float = 30.0
    # Namespacing salt mixed into every flag's rollout bucketing so bucket
    # assignments are stable for THIS deployment and uncorrelated across flags.
    flags_default_salt: str = "kinora"
    # Redis pub/sub channel the cache broadcasts invalidations on.
    flags_stream_channel: str = "kinora:flags:invalidate"

    # --- Notifications & webhooks platform (§5 events / §12 reliability) ---
    # Out-of-band delivery is OFF by default: with no real transport configured the
    # platform still runs end-to-end (in-app inbox + logging transports), spends no
    # credits, and sends no real email/push. These knobs tune the §12.1 retry +
    # circuit-breaker behaviour for outbound webhook / email / push delivery.
    notify_retry_max_attempts: int = 5
    notify_retry_base_s: float = 2.0
    notify_retry_factor: float = 4.0
    notify_retry_max_delay_s: float = 300.0
    notify_circuit_failure_threshold: int = 5
    notify_circuit_reset_timeout_s: float = 30.0
    #: HMAC replay tolerance a receiver should enforce on signed webhooks (seconds).
    notify_webhook_tolerance_s: int = 300
    #: Run the live-event → durable-notification bridge in-process (API role).
    #: Subscribes to the existing §5.6 channels and emits notifications for the
    #: notifiable events (book ready, render done, budget low, conflict surfaced).
    notify_bridge_enabled: bool = True

    # --- CORS ---
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- Reports subsystem (additive) ---
    # Emails allowed to generate operator-facing reports (budget/quality/
    # throughput/library). Empty + ``local`` ⇒ any authenticated user may (dev
    # convenience); empty + non-local ⇒ nobody, so operator dashboards are
    # locked down by default in production until an allowlist is configured.
    report_operator_emails: list[str] = []
    # Signed report-download URL lifetime (seconds).
    report_url_ttl_s: int = 3600

    # --- Third-party integrations & import (app.integrations; all optional) ---
    # Token at-rest sealing key. When set (and ``cryptography`` is installed) the
    # OAuth/token blobs in ``app_connections`` are Fernet-encrypted; absent it a
    # clearly-labelled reversible fallback is used (fine for local dev/tests).
    integrations_encryption_key: str | None = None
    # Per-sync caps (cost + safety). A single sync imports at most this many items.
    integrations_max_items_per_sync: int = 500
    # Consecutive failed syncs before a connection is flipped to ERROR health.
    integrations_error_threshold: int = 3
    # OAuth2 client credentials per provider (only the ones you enable need set).
    # Endpoints have sane public defaults; only the id/secret are secrets.
    notion_oauth_client_id: str | None = None
    notion_oauth_client_secret: str | None = None
    pocket_oauth_client_id: str | None = None
    pocket_oauth_client_secret: str | None = None
    # The redirect URI registered with the providers (the app's OAuth callback).
    integrations_oauth_redirect_uri: str = "http://localhost:8000/api/integrations/oauth/callback"
    # Optional per-provider webhook signing secrets (push-based sync).
    readwise_webhook_secret: str | None = None
    notion_webhook_secret: str | None = None

    # --- Async-video/audio provider webhook ingress (app.video.webhooks; §12.1) ---
    # The production HTTP door async media providers post render-completion
    # callbacks to (``POST /api/video/webhooks/{provider}``). The route is
    # unauthenticated by design — the provider's *signature* is the auth — so a
    # per-provider signing secret must be set for that provider to be accepted; an
    # unset provider yields a clean 404 (the gateway ships dark and lights up per
    # provider). No secret is required to boot. None of these spend credits or
    # touch the live model path.
    video_webhook_wan_secret: str | None = None
    video_webhook_dashscope_secret: str | None = None
    video_webhook_minimax_secret: str | None = None
    #: Signing secret for callbacks our own internal services post (canonical shape).
    video_webhook_internal_secret: str | None = None
    #: HMAC replay tolerance (seconds) the ingress enforces on a signed timestamp.
    video_webhook_tolerance_s: int = 300
    #: Hard cap on an inbound callback body (bytes); larger ⇒ 413 before parsing.
    video_webhook_max_body_bytes: int = 1 * 1024 * 1024
    #: Per-source (provider+IP) token-bucket rate limit for the ingress route.
    video_webhook_rate_capacity: int = 120
    video_webhook_rate_refill_per_s: float = 4.0

    # --- Content translation (app.translation; distinct from UI i18n) ---
    # The content-translation subsystem translates reader-facing material (page
    # text, canon entity descriptions, narration scripts) into a reader's
    # language. It is token-only (never renders video) and behind an injectable
    # provider, so it stays OFF the live model path in tests. Defaults only —
    # nothing here is required, and the subsystem is built lazily.
    translation_enabled: bool = True
    # The chat model id used by the LLM-backed translation provider; reuses the
    # shared chat seam (DashScope/OpenAI per ``reasoning_provider``).
    translation_model: str = "qwen3.7-plus"
    # Quality estimate below which a segment is flagged for human post-edit.
    translation_review_threshold: float = 0.7
    # Translation-memory fuzzy-match similarity floor (reuse a near-identical
    # prior translation as a suggestion instead of paying for a fresh call).
    translation_fuzzy_threshold: float = 0.82
    # Batch packing bounds for provider calls (count + estimated tokens).
    translation_max_batch_size: int = 32
    translation_max_batch_tokens: int = 6000

    # --- Billing & payments (additive; owned by app.billing) ---
    # The billing domain is the commercial mirror of the §11 video-seconds budget:
    # it meters reader consumption (reading-minutes / render-seconds) and turns it
    # into subscriptions + invoices. The payment provider is ALWAYS the in-memory
    # fake here — no real Stripe/network/payment call is ever made — so the only
    # required value is the webhook signing secret, which has a safe local default.
    billing_provider: str = "fake"  # "fake" only; a real Stripe transport is unwired
    billing_default_currency: str = "USD"
    billing_invoice_prefix: str = "KIN"
    billing_webhook_secret: str = "whsec_kinora_local_dev_secret"  # noqa: S105 - local dev default
    billing_webhook_tolerance_s: int = 300
    # Comma-separated retry cadence (days) for dunning on a failed payment.
    billing_dunning_retry_days: str = "1,3,5,7"
    # Whether finalizing a positive invoice immediately attempts payment.
    billing_auto_charge_on_finalize: bool = True

    # --- LLM-ops / prompt registry (app.llmops; additive, safe defaults) ---
    # The LLM-ops platform (versioned prompt registry, eval/A-B harness,
    # injection/jailbreak defense, model registry, run tracing, response cache)
    # is read-only-by-default and seeds itself from app.agents.prompts without
    # editing them. All settings default to a conservative, offline posture.
    #
    #: Master switch for the /api/llmops surface. OFF keeps the API unchanged.
    llmops_enabled: bool = False
    #: Always sanitize untrusted input text (fence it as data) — defense in depth.
    llmops_guardrail_always_sanitize: bool = True
    #: Injection score at/above which an input is BLOCKED (vs merely sanitized).
    llmops_injection_block_score: float = 0.85
    #: Response-cache TTL (seconds) and max in-memory entries.
    llmops_cache_ttl_s: float = 3600.0
    llmops_cache_max_entries: int = 2048
    #: In-memory run-trace ring-buffer capacity (per process).
    llmops_trace_capacity: int = 10_000
    #: Default number of eval/A-B runs (the §13 "mean + spread over N" knob).
    llmops_eval_runs: int = 3

    # --- Input-validation & data-hygiene hardening (app.sechardening; additive) ---
    # Defensive upload / storage / download / log-redaction settings.  All
    # defaults are safe-conservative and can be tightened via env vars.

    #: Maximum size (bytes) for any single file upload.  Default 100 MiB.
    sechardening_upload_max_bytes: int = 100 * 1_024 * 1_024

    #: Maximum *character* length for a raw storage key before normalization.
    sechardening_key_max_raw_chars: int = 2_048

    #: Comma-separated list of trusted download domains.  An empty string means
    #: ``app.sechardening.domains.DEFAULT_ALLOWED_DOMAINS`` is used.  Entries
    #: use suffix-match semantics: ``"dashscope.com"`` also allows
    #: ``"cdn.dashscope.com"``.
    sechardening_allowed_download_domains: str = ""

    #: Comma-separated list of additional log-event key names (exact, case-
    #: insensitive) that the redaction processor should treat as sensitive,
    #: beyond the built-in vocabulary.
    sechardening_extra_redact_keys: str = ""

    @property
    def sechardening_allowed_domains_list(self) -> tuple[str, ...]:
        """Parsed tuple of trusted download domains from config.

        Returns the configured list when non-empty, or the library default.
        """
        from app.sechardening.domains import DEFAULT_ALLOWED_DOMAINS

        raw = self.sechardening_allowed_download_domains.strip()
        if not raw:
            return DEFAULT_ALLOWED_DOMAINS
        return tuple(d.strip().lower() for d in raw.split(",") if d.strip())

    @property
    def sechardening_extra_redact_keys_tuple(self) -> tuple[str, ...]:
        """Parsed tuple of extra sensitive key names for log redaction."""
        raw = self.sechardening_extra_redact_keys.strip()
        if not raw:
            return ()
        return tuple(k.strip().lower() for k in raw.split(",") if k.strip())
    # --- Chaos / game-day framework (app/chaos) ---
    #: Explicit opt-in to arm orchestrated chaos faults. OFF by default. Even when
    #: set, the production hard gate (app/chaos/gate.py) refuses to arm unless
    #: ``app_env`` is a chaos-safe environment (``local``/``test``/``ci``), so a
    #: stray ``CHAOS_ENABLED=true`` in prod still cannot inject faults.
    chaos_enabled: bool = False

    @property
    def is_local(self) -> bool:
        """True when running in the local development environment."""
        return self.app_env.lower() == "local"

    def is_report_operator(self, email: str) -> bool:
        """Whether ``email`` may generate operator-facing reports.

        Locked down by default outside ``local``: an empty allowlist denies all
        operator reports in production but permits them in local development so
        the dashboards are usable out of the box.
        """
        allow = {e.strip().lower() for e in self.report_operator_emails if e.strip()}
        if allow:
            return email.strip().lower() in allow
        return self.is_local

    @property
    def analytics_salt_effective(self) -> str:
        """The salt to feed the analytics scrubber (falls back to the JWT secret)."""
        return self.analytics_salt or self.jwt_secret

    @property
    def audit_redaction_salt_effective(self) -> str:
        """The salt for audit PII-redaction commitments (falls back to JWT secret)."""
        return self.audit_redaction_salt or self.jwt_secret

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """Refuse to boot with the insecure default JWT secret outside ``local``.

        ``JWT_SECRET`` defaults to a well-known placeholder so the app runs out of
        the box locally; shipping that placeholder to any non-local environment
        would let anyone mint valid tokens, so we hard-fail at settings load
        (which fails ``create_app`` / the lifespan / every entrypoint).
        """
        if not self.is_local and self.jwt_secret == DEFAULT_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET must be set to a real secret when APP_ENV is not 'local' "
                "(refusing to boot with the insecure default 'change-me-in-prod')."
            )
        if not self.is_local and self.api_key_pepper == DEFAULT_API_KEY_PEPPER:
            # A dedicated API_KEY_PEPPER is preferred, but rather than add a second
            # mandatory secret to every deployment we derive one from the (already
            # mandatory, already-real) JWT secret so the stored API-key HMAC is
            # never keyed by the well-known placeholder. Setting API_KEY_PEPPER
            # explicitly still overrides this (it differs from the default).
            import hashlib

            object.__setattr__(
                self,
                "api_key_pepper",
                "derived:" + hashlib.sha256(("apikey:" + self.jwt_secret).encode()).hexdigest(),
            )
        return self

    @model_validator(mode="after")
    def _guard_reasoning_provider(self) -> Settings:
        """Validate the reasoning-provider toggle and its required credentials."""
        provider = self.reasoning_provider.lower()
        if provider not in {"dashscope", "openai"}:
            raise ValueError(
                "REASONING_PROVIDER must be 'dashscope' or 'openai', "
                f"got {self.reasoning_provider!r}."
            )
        if provider == "openai" and not self.openai_api_key:
            raise ValueError("REASONING_PROVIDER='openai' requires OPENAI_API_KEY to be set.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance."""
    return Settings()
