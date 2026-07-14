"""Startup config validator — cross-field invariants over the live Settings.

:class:`~app.core.config.Settings` already self-validates *single-field* shapes
(types, the JWT-secret production guard, the reasoning-provider toggle). This
module adds the **cross-field** layer: invariants that span several settings and
that a type checker can't express — "if live video is armed there must be a video
key *and* a USD cap", "if ``video_backend=minimax`` then ``minimax_api_key`` is
present", "the S3 public-base-url must be coherent with the endpoint", "the
scheduler watermarks must be ordered". Each invariant is a pure function from
``Settings`` to a list of :class:`ConfigFinding`; the validator runs them all and
rolls the worst severity into a :class:`ReadinessVerdict`.

Crucially this is **non-fatal**: it never raises and never enables anything. It
reports. The production-safety gate (:mod:`app.configmgmt.safety`) is the only
component that turns findings into a boot refusal, and it consumes this verdict.

``KINORA_LIVE_VIDEO`` is treated strictly as a *guarded gate*: the validator
checks that, **if** it is on, the spend guards are configured — it never advises
turning it on and flags it as a warning everywhere it is armed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.configmgmt.errors import ConfigFinding, Severity
from app.core.config import DEFAULT_API_KEY_PEPPER, DEFAULT_JWT_SECRET

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "ReadinessVerdict",
    "ConfigValidator",
    "validate_settings",
    "INVARIANTS",
]


# --------------------------------------------------------------------------- #
# Individual invariants. Each is pure: Settings -> list[ConfigFinding].
# A finding is emitted only when the invariant is *violated*; a satisfied
# invariant yields []. Codes are namespaced ("<area>.<problem>") and stable.
# --------------------------------------------------------------------------- #

#: An invariant maps the live Settings to zero or more findings.
Invariant = Callable[["Settings"], list[ConfigFinding]]


def _check_live_video_gate(s: Settings) -> list[ConfigFinding]:
    """KINORA_LIVE_VIDEO is a guarded spend gate; if armed, guards must be set.

    We never recommend enabling it. When it *is* on we (a) warn unconditionally
    that real spend is possible and (b) require a positive USD ceiling and a
    configured video provider key so the belt-and-suspenders guard can fire.
    """
    out: list[ConfigFinding] = []
    if not s.kinora_live_video:
        return out

    out.append(
        ConfigFinding(
            code="live_video.armed",
            severity=Severity.WARNING,
            message=(
                "KINORA_LIVE_VIDEO is ON — the render pipeline may spend real " "video credits."
            ),
            fields=("kinora_live_video",),
            hint="leave this OFF unless you intend to spend; it is a guarded gate",
        )
    )
    if s.budget_ceiling_usd <= 0:
        out.append(
            ConfigFinding(
                code="live_video.no_usd_cap",
                severity=Severity.ERROR,
                message=(
                    "Live video is armed but BUDGET_CEILING_USD is not a positive "
                    "spend ceiling; the USD spend guard cannot protect against drift."
                ),
                fields=("kinora_live_video", "budget_ceiling_usd"),
                hint="set BUDGET_CEILING_USD > 0",
            )
        )
    if s.budget_ceiling_video_s <= 0:
        out.append(
            ConfigFinding(
                code="live_video.no_seconds_cap",
                severity=Severity.ERROR,
                message=(
                    "Live video is armed but the video-seconds ceiling is not "
                    "positive; the primary budget ledger has no ceiling to enforce."
                ),
                fields=("kinora_live_video", "budget_ceiling_video_s"),
                hint="set BUDGET_CEILING_VIDEO_S > 0",
            )
        )
    # The active video backend must have a usable key when live.
    backend = s.video_backend.lower()
    if backend == "minimax" and not s.minimax_api_key:
        out.append(
            ConfigFinding(
                code="live_video.minimax_no_key",
                severity=Severity.ERROR,
                message="Live video uses the MiniMax backend but MINIMAX_API_KEY is unset.",
                fields=("kinora_live_video", "video_backend", "minimax_api_key"),
                hint="set MINIMAX_API_KEY",
            )
        )
    if backend == "dashscope" and not s.dashscope_api_key:
        # dashscope_api_key has no default (required field) so this is rare, but
        # an empty string slips past the required check — guard it explicitly.
        out.append(
            ConfigFinding(
                code="live_video.dashscope_no_key",
                severity=Severity.ERROR,
                message="Live video uses the DashScope backend but DASHSCOPE_API_KEY is empty.",
                fields=("kinora_live_video", "video_backend", "dashscope_api_key"),
                hint="set DASHSCOPE_API_KEY",
            )
        )
    return out


def _check_video_backend(s: Settings) -> list[ConfigFinding]:
    """``video_backend`` must be a known provider, with its key present if minimax."""
    out: list[ConfigFinding] = []
    backend = s.video_backend.lower()
    if backend not in {"dashscope", "minimax"}:
        out.append(
            ConfigFinding(
                code="video_backend.unknown",
                severity=Severity.ERROR,
                message=f"VIDEO_BACKEND must be 'dashscope' or 'minimax', got {s.video_backend!r}.",
                fields=("video_backend",),
            )
        )
        return out
    # Selecting minimax without a key is a misconfiguration even when live video
    # is OFF (the backend would fail on first use); ERROR if armed, else WARNING.
    if backend == "minimax" and not s.minimax_api_key:
        out.append(
            ConfigFinding(
                code="video_backend.minimax_no_key",
                severity=Severity.ERROR if s.kinora_live_video else Severity.WARNING,
                message="VIDEO_BACKEND='minimax' but MINIMAX_API_KEY is unset.",
                fields=("video_backend", "minimax_api_key"),
                hint="set MINIMAX_API_KEY",
            )
        )
    if backend == "minimax" and s.minimax_cost_per_clip_usd <= 0:
        out.append(
            ConfigFinding(
                code="video_backend.minimax_zero_cost",
                severity=Severity.WARNING,
                message=(
                    "MINIMAX_COST_PER_CLIP_USD is not positive; the per-clip USD "
                    "spend accounting will under-count."
                ),
                fields=("minimax_cost_per_clip_usd",),
            )
        )
    return out


def _check_reasoning_provider(s: Settings) -> list[ConfigFinding]:
    """Reasoning provider must be known and have its key when 'openai'.

    Settings already hard-validates this (raises). We re-report it as a finding
    so a readiness verdict is complete even if a caller built Settings in a way
    that bypassed the model validator (e.g. constructing a stub for inspection).
    """
    out: list[ConfigFinding] = []
    provider = s.reasoning_provider.lower()
    if provider not in {"dashscope", "openai"}:
        out.append(
            ConfigFinding(
                code="reasoning.unknown_provider",
                severity=Severity.ERROR,
                message=(
                    "REASONING_PROVIDER must be 'dashscope' or 'openai', "
                    f"got {s.reasoning_provider!r}."
                ),
                fields=("reasoning_provider",),
            )
        )
    elif provider == "openai" and not s.openai_api_key:
        out.append(
            ConfigFinding(
                code="reasoning.openai_no_key",
                severity=Severity.ERROR,
                message="REASONING_PROVIDER='openai' requires OPENAI_API_KEY.",
                fields=("reasoning_provider", "openai_api_key"),
                hint="set OPENAI_API_KEY",
            )
        )
    return out


def _check_s3_coherence(s: Settings) -> list[ConfigFinding]:
    """S3 endpoint / public-base-url / bucket must be coherent.

    A missing public base URL outside ``local`` means browser-reachable media
    links fall back to the in-cluster endpoint, which is not reachable from a
    browser — a real production foot-gun (the AGENTS.md rewrite note). A blank
    bucket is always an error.
    """
    out: list[ConfigFinding] = []
    if not s.s3_bucket.strip():
        out.append(
            ConfigFinding(
                code="s3.no_bucket",
                severity=Severity.ERROR,
                message="S3_BUCKET is empty; object storage cannot be addressed.",
                fields=("s3_bucket",),
            )
        )
    if not s.s3_endpoint_url.strip():
        out.append(
            ConfigFinding(
                code="s3.no_endpoint",
                severity=Severity.ERROR,
                message="S3_ENDPOINT_URL is empty.",
                fields=("s3_endpoint_url",),
            )
        )
    if not s.is_local and not s.s3_public_base_url:
        out.append(
            ConfigFinding(
                code="s3.no_public_base_url",
                severity=Severity.WARNING,
                message=(
                    "S3_PUBLIC_BASE_URL is unset outside local; media links will "
                    "use the internal endpoint and may not be browser-reachable."
                ),
                fields=("s3_public_base_url", "s3_endpoint_url"),
                hint="set S3_PUBLIC_BASE_URL to the browser-reachable media base",
            )
        )
    # A localhost public base URL outside local is almost certainly wrong.
    if (
        not s.is_local
        and s.s3_public_base_url
        and ("localhost" in s.s3_public_base_url or "127.0.0.1" in s.s3_public_base_url)
    ):
        out.append(
            ConfigFinding(
                code="s3.localhost_public_url",
                severity=Severity.WARNING,
                message="S3_PUBLIC_BASE_URL points at localhost outside a local environment.",
                fields=("s3_public_base_url",),
            )
        )
    return out


def _check_scheduler_watermarks(s: Settings) -> list[ConfigFinding]:
    """Watermarks/horizons must be ordered (low<high, commit<spec) and positive."""
    out: list[ConfigFinding] = []
    if s.watermark_low_s >= s.watermark_high_s:
        out.append(
            ConfigFinding(
                code="scheduler.watermarks_unordered",
                severity=Severity.ERROR,
                message=(
                    f"WATERMARK_LOW_S ({s.watermark_low_s}) must be < "
                    f"WATERMARK_HIGH_S ({s.watermark_high_s})."
                ),
                fields=("watermark_low_s", "watermark_high_s"),
            )
        )
    if s.commit_horizon_s >= s.spec_horizon_s:
        out.append(
            ConfigFinding(
                code="scheduler.horizons_unordered",
                severity=Severity.ERROR,
                message=(
                    f"COMMIT_HORIZON_S ({s.commit_horizon_s}) must be < "
                    f"SPEC_HORIZON_S ({s.spec_horizon_s})."
                ),
                fields=("commit_horizon_s", "spec_horizon_s"),
            )
        )
    return out


def _check_budget_ordering(s: Settings) -> list[ConfigFinding]:
    """Per-scope budgets must be positive and nested sensibly under the ceiling."""
    out: list[ConfigFinding] = []
    for name, value in (
        ("budget_ceiling_video_s", s.budget_ceiling_video_s),
        ("budget_per_session_s", s.budget_per_session_s),
        ("budget_per_scene_s", s.budget_per_scene_s),
    ):
        if value <= 0:
            out.append(
                ConfigFinding(
                    code="budget.non_positive",
                    severity=Severity.WARNING,
                    message=f"{name.upper()} should be positive (got {value}).",
                    fields=(name,),
                )
            )
    if s.budget_per_session_s > s.budget_ceiling_video_s:
        out.append(
            ConfigFinding(
                code="budget.session_exceeds_ceiling",
                severity=Severity.WARNING,
                message=(
                    "BUDGET_PER_SESSION_S exceeds the global video-seconds ceiling; "
                    "a single session could consume the whole budget."
                ),
                fields=("budget_per_session_s", "budget_ceiling_video_s"),
            )
        )
    return out


def _check_finops_fractions(s: Settings) -> list[ConfigFinding]:
    """FinOps alert fractions must be non-decreasing and within (0, 1]."""
    out: list[ConfigFinding] = []
    info, warn, soft = (
        s.finops_alert_info_fraction,
        s.finops_alert_warning_fraction,
        s.finops_soft_cap_fraction,
    )
    if not (0.0 < info <= warn <= soft <= 1.0):
        out.append(
            ConfigFinding(
                code="finops.fractions_unordered",
                severity=Severity.ERROR,
                message=(
                    "FinOps alert fractions must satisfy 0 < info <= warning <= "
                    f"soft <= 1 (got info={info}, warning={warn}, soft={soft})."
                ),
                fields=(
                    "finops_alert_info_fraction",
                    "finops_alert_warning_fraction",
                    "finops_soft_cap_fraction",
                ),
            )
        )
    return out


def _check_embed_dim(s: Settings) -> list[ConfigFinding]:
    """The pgvector embedding dimension must be positive (DB-coupled)."""
    out: list[ConfigFinding] = []
    if s.embed_dim <= 0:
        out.append(
            ConfigFinding(
                code="embed.bad_dim",
                severity=Severity.ERROR,
                message=f"EMBED_DIM must be positive (got {s.embed_dim}).",
                fields=("embed_dim",),
            )
        )
    return out


def _check_log_level(s: Settings) -> list[ConfigFinding]:
    """LOG_LEVEL must be a recognised stdlib level name."""
    out: list[ConfigFinding] = []
    valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
    if s.log_level.upper() not in valid:
        out.append(
            ConfigFinding(
                code="logging.bad_level",
                severity=Severity.WARNING,
                message=f"LOG_LEVEL {s.log_level!r} is not a standard level; defaults to INFO.",
                fields=("log_level",),
            )
        )
    return out


def _check_secret_placeholders(s: Settings) -> list[ConfigFinding]:
    """Insecure default secrets are fine locally but flagged everywhere else."""
    out: list[ConfigFinding] = []
    if s.jwt_secret == DEFAULT_JWT_SECRET:
        out.append(
            ConfigFinding(
                code="secrets.default_jwt",
                severity=Severity.INFO if s.is_local else Severity.ERROR,
                message="JWT_SECRET is the insecure built-in placeholder.",
                fields=("jwt_secret",),
                hint="set a real JWT_SECRET",
            )
        )
    if s.api_key_pepper == DEFAULT_API_KEY_PEPPER:
        out.append(
            ConfigFinding(
                code="secrets.default_pepper",
                # Settings derives a pepper from the JWT secret outside local, so
                # this is INFO there (covered) but still worth surfacing.
                severity=Severity.INFO,
                message=(
                    "API_KEY_PEPPER is the built-in placeholder (a value is "
                    "derived from JWT_SECRET outside local)."
                ),
                fields=("api_key_pepper",),
            )
        )
    return out


def _check_cors(s: Settings) -> list[ConfigFinding]:
    """A wildcard CORS origin outside local is a security smell."""
    out: list[ConfigFinding] = []
    if not s.is_local and "*" in s.cors_origins:
        out.append(
            ConfigFinding(
                code="cors.wildcard",
                severity=Severity.WARNING,
                message=(
                    "CORS_ORIGINS contains '*' outside local; this disables " "origin restriction."
                ),
                fields=("cors_origins",),
            )
        )
    return out


def _check_mcp_auth(s: Settings) -> list[ConfigFinding]:
    """The HTTP MCP control surface must be authenticated outside local."""
    out: list[ConfigFinding] = []
    if not s.is_local and not s.mcp_auth_token and not s.mcp_client_scopes:
        out.append(
            ConfigFinding(
                code="mcp.unauthenticated",
                severity=Severity.ERROR,
                message=(
                    "MCP_AUTH_TOKEN (or MCP_CLIENT_SCOPES) is unset outside local; "
                    "an unauthenticated canon-memory control surface must never run in prod."
                ),
                fields=("mcp_auth_token", "mcp_client_scopes"),
                hint="set MCP_AUTH_TOKEN",
            )
        )
    return out


#: The ordered invariant suite the validator runs. Adding a check is a one-line
#: append; tests assert each fires on a crafted Settings and passes on a clean one.
INVARIANTS: tuple[Invariant, ...] = (
    _check_live_video_gate,
    _check_video_backend,
    _check_reasoning_provider,
    _check_s3_coherence,
    _check_scheduler_watermarks,
    _check_budget_ordering,
    _check_finops_fractions,
    _check_embed_dim,
    _check_log_level,
    _check_secret_placeholders,
    _check_cors,
    _check_mcp_auth,
)


@dataclass(frozen=True, slots=True)
class ReadinessVerdict:
    """The roll-up of one validation pass.

    Args:
        env: The environment the verdict was computed for (``settings.app_env``).
        findings: Every finding produced, in invariant order.
    """

    env: str
    findings: tuple[ConfigFinding, ...]

    @property
    def max_severity(self) -> Severity | None:
        """The worst severity present (``None`` when there are no findings)."""
        return max((f.severity for f in self.findings), default=None)

    @property
    def is_ready(self) -> bool:
        """True when nothing blocks a clean boot (no ERROR/FATAL findings)."""
        return not any(f.is_blocking for f in self.findings)

    def of_severity(self, severity: Severity) -> tuple[ConfigFinding, ...]:
        """Findings at exactly ``severity``."""
        return tuple(f for f in self.findings if f.severity == severity)

    @property
    def errors(self) -> tuple[ConfigFinding, ...]:
        return tuple(f for f in self.findings if f.severity >= Severity.ERROR)

    @property
    def warnings(self) -> tuple[ConfigFinding, ...]:
        return self.of_severity(Severity.WARNING)

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly verdict for a readiness/health surface."""
        return {
            "env": self.env,
            "ready": self.is_ready,
            "max_severity": self.max_severity.label if self.max_severity else None,
            "counts": {
                sev.label: len(self.of_severity(sev))
                for sev in (Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.FATAL)
            },
            "findings": [f.to_dict() for f in self.findings],
        }


class ConfigValidator:
    """Runs the cross-field invariant suite over a :class:`Settings`."""

    def __init__(self, invariants: tuple[Invariant, ...] = INVARIANTS) -> None:
        self._invariants = invariants

    def validate(self, settings: Settings) -> ReadinessVerdict:
        """Run every invariant and roll the results into a verdict."""
        findings: list[ConfigFinding] = []
        for invariant in self._invariants:
            findings.extend(invariant(settings))
        return ReadinessVerdict(env=settings.app_env, findings=tuple(findings))


def validate_settings(settings: Settings) -> ReadinessVerdict:
    """Module-level convenience: validate ``settings`` with the default suite."""
    return ConfigValidator().validate(settings)
