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

    # --- Wan task polling ---
    video_poll_timeout_s: float = 600.0
    video_poll_interval_s: float = 3.0
    video_poll_max_interval_s: float = 15.0

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

    # --- Live video go-live gate ---
    kinora_live_video: bool = False

    # --- Concurrency lanes ---
    concurrency_committed: int = 4
    concurrency_speculative: int = 2
    concurrency_keyframe: int = 2
    retry_cap: int = 2

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

    # --- Ingest recovery ---
    ingest_recovery_interval_s: float = 30.0
    ingest_recovery_limit: int = 25

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
