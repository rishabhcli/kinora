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

    # --- Model ids ---
    chat_model_max: str = "qwen3.7-max"
    chat_model_plus: str = "qwen3.7-plus"
    chat_model_adapter: str = "qwen3.5-plus"
    vl_model: str = "qwen-vl-max"
    image_model: str = "qwen-image-2.0-pro"
    image_edit_model: str = "qwen-image-edit-max"
    # Default narration TTS model. qwen3-tts-flash serves the PRESET voices that
    # ingest.identity_lock assigns (e.g. "Cherry", "Ryan"), so preset-voice
    # narration works out of the box. qwen3-tts-vc is the separate voice-CLONE
    # model (it rejects preset voice ids); clone callers pass it explicitly via
    # TtsProvider.clone_voice(target_model="qwen3-tts-vc").
    tts_model: str = "qwen3-tts-flash"
    # NOTE: the Wan video model ids are placeholders to be confirmed in the providers phase.
    video_model: str = "wan2.7-t2v"
    video_model_i2v: str = "wan2.7-i2v"
    video_model_r2v: str = "wan2.7-r2v"

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
    cors_origins: list[str] = ["http://localhost:5173"]

    @property
    def is_local(self) -> bool:
        """True when running in the local development environment."""
        return self.app_env.lower() == "local"

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


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance."""
    return Settings()
