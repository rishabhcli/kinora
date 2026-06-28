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

    # --- Live video go-live gate ---
    kinora_live_video: bool = False

    # --- Concurrency lanes ---
    concurrency_committed: int = 4
    concurrency_speculative: int = 2
    concurrency_keyframe: int = 2
    retry_cap: int = 2

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

    # --- Auth (JWT) ---
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_alg: str = "HS256"
    access_token_ttl_s: int = 86400

    # --- MCP control surface (the deployed canon-memory server, §8.3/§14) ---
    # When set, the streamable-HTTP MCP requires ``Authorization: Bearer <token>``.
    # Outside ``local`` it is mandatory: the HTTP MCP refuses to start without it
    # (an unauthenticated control surface must never run in prod, §12).
    mcp_auth_token: str | None = None

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
