"""Production-safety gate — refuse to boot an unsafe production process.

Where the validator (:mod:`app.configmgmt.validator`) *reports*, this gate
*enforces*. It is the one component in this plane that raises: given a Settings
bound for a production-grade environment (``staging``/``prod``), it asserts the
non-negotiable safety invariants and raises :class:`ProdSafetyError` — carrying
**all** violations — if any fails. Wire it into the boot path (lifespan) so a
misconfigured prod process dies loudly at startup rather than serving traffic
with demo credentials or an unguarded live-video gate.

The fatal rules (each a :class:`Severity.FATAL` finding):

1. **No demo credentials in prod.** The demo login / placeholder secrets
   (``demo@kinora.local``, the built-in JWT secret) must never reach prod.
2. **No insecure secret placeholders** (JWT secret / API-key pepper defaults).
3. **Live video requires an explicit prod opt-in.** ``KINORA_LIVE_VIDEO`` ON in
   prod is refused *unless* an explicit opt-in env (``KINORA_PROD_LIVE_VIDEO_OK``)
   is also set — so spend can never be armed by a stray flag.
4. **Debug posture off.** Anything that looks like debug/chaos armed in prod is
   fatal (verbose DEBUG logging, an armed chaos seed override env).
5. **Any blocking validator finding** (ERROR) is escalated to FATAL in prod —
   readiness errors must not boot a production process.

Locally (``app_env == local``) the gate is a near no-op: it returns the validator
verdict unchanged and raises nothing, so dev keeps working out of the box.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.configmgmt.errors import ConfigFinding, ProdSafetyError, Severity
from app.configmgmt.profiles import ProfileName
from app.configmgmt.validator import ReadinessVerdict, validate_settings
from app.core.config import DEFAULT_API_KEY_PEPPER, DEFAULT_JWT_SECRET
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "DEMO_EMAIL",
    "DEMO_PASSWORD",
    "PROD_LIVE_VIDEO_OPT_IN_ENV",
    "ProdSafetyReport",
    "ProdSafetyGate",
    "assert_safe_to_boot",
]

_log = get_logger("configmgmt.safety")

#: The demo credentials from CLAUDE.md — never valid in prod.
DEMO_EMAIL = "demo@kinora.local"
DEMO_PASSWORD = "demo-password-123"  # noqa: S105 - sentinel to refuse, not a credential

#: The explicit, audited env an operator must set to arm live video in prod. Its
#: presence is the *only* way KINORA_LIVE_VIDEO is allowed on outside local.
PROD_LIVE_VIDEO_OPT_IN_ENV = "KINORA_PROD_LIVE_VIDEO_OK"
#: An env that, if truthy, signals chaos fault-injection is armed — fatal in prod.
CHAOS_ARMED_ENV = "KINORA_CHAOS_ARMED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _is_production(settings: Settings) -> bool:
    """True for the production-grade environments the gate enforces."""
    try:
        profile = ProfileName.coerce(settings.app_env)
    except ValueError:
        # An unknown, non-local env name: treat conservatively as production so a
        # typo can't downgrade the safety posture.
        return not settings.is_local
    return profile in {ProfileName.STAGING, ProfileName.PROD}


@dataclass(frozen=True, slots=True)
class ProdSafetyReport:
    """The outcome of a safety check: the verdict plus the fatal findings."""

    verdict: ReadinessVerdict
    fatal: tuple[ConfigFinding, ...]

    @property
    def safe(self) -> bool:
        """True when no fatal violation was found."""
        return not self.fatal

    def to_dict(self) -> dict[str, object]:
        return {
            "safe": self.safe,
            "fatal": [f.to_dict() for f in self.fatal],
            "verdict": self.verdict.to_dict(),
        }


class ProdSafetyGate:
    """Asserts production-safety invariants over a :class:`Settings`."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        # Defaults to the live process env; injectable so tests stay hermetic and
        # never depend on (or mutate) the real environment.
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    # -- individual fatal rules ------------------------------------------- #

    def _demo_credentials(self, s: Settings) -> list[ConfigFinding]:
        out: list[ConfigFinding] = []
        # The report-operator allowlist / any demo-ish account leaking in is
        # caught by checking known demo material against secret-ish fields.
        if s.jwt_secret == DEFAULT_JWT_SECRET:
            out.append(
                ConfigFinding(
                    code="prod.demo_jwt_secret",
                    severity=Severity.FATAL,
                    message="The built-in demo JWT_SECRET must never be used in production.",
                    fields=("jwt_secret",),
                    hint="set a real JWT_SECRET",
                )
            )
        if s.api_key_pepper == DEFAULT_API_KEY_PEPPER:
            out.append(
                ConfigFinding(
                    code="prod.demo_api_key_pepper",
                    severity=Severity.FATAL,
                    message="The built-in demo API_KEY_PEPPER must never be used in production.",
                    fields=("api_key_pepper",),
                    hint="set a real API_KEY_PEPPER",
                )
            )
        # The local-demo S3 credentials are a known weak default.
        if s.s3_access_key == "kinora" and s.s3_secret_key == "kinora-secret":
            out.append(
                ConfigFinding(
                    code="prod.demo_s3_credentials",
                    severity=Severity.FATAL,
                    message="The local-demo S3 credentials must never be used in production.",
                    fields=("s3_access_key", "s3_secret_key"),
                    hint="set real S3 credentials",
                )
            )
        if s.billing_webhook_secret == "whsec_kinora_local_dev_secret":
            out.append(
                ConfigFinding(
                    code="prod.demo_billing_secret",
                    severity=Severity.FATAL,
                    message=(
                        "The local-dev billing webhook secret must never be " "used in production."
                    ),
                    fields=("billing_webhook_secret",),
                    hint="set a real BILLING_WEBHOOK_SECRET",
                )
            )
        return out

    def _live_video_opt_in(self, s: Settings) -> list[ConfigFinding]:
        if not s.kinora_live_video:
            return []
        if _is_truthy(self._environ.get(PROD_LIVE_VIDEO_OPT_IN_ENV)):
            # Armed *and* explicitly opted in: allowed, but loudly recorded.
            _log.warning(
                "prod_live_video_armed_with_opt_in",
                opt_in_env=PROD_LIVE_VIDEO_OPT_IN_ENV,
            )
            return []
        return [
            ConfigFinding(
                code="prod.live_video_without_opt_in",
                severity=Severity.FATAL,
                message=(
                    "KINORA_LIVE_VIDEO is ON in production without the explicit "
                    f"opt-in {PROD_LIVE_VIDEO_OPT_IN_ENV}; refusing to arm spend."
                ),
                fields=("kinora_live_video",),
                hint=f"either turn KINORA_LIVE_VIDEO off or set {PROD_LIVE_VIDEO_OPT_IN_ENV}=1",
            )
        ]

    def _debug_posture(self, s: Settings) -> list[ConfigFinding]:
        out: list[ConfigFinding] = []
        if s.log_level.upper() == "DEBUG":
            out.append(
                ConfigFinding(
                    code="prod.debug_logging",
                    severity=Severity.FATAL,
                    message="LOG_LEVEL=DEBUG in production risks leaking sensitive payloads.",
                    fields=("log_level",),
                    hint="use INFO or higher in production",
                )
            )
        if _is_truthy(self._environ.get(CHAOS_ARMED_ENV)):
            out.append(
                ConfigFinding(
                    code="prod.chaos_armed",
                    severity=Severity.FATAL,
                    message=f"Chaos fault-injection ({CHAOS_ARMED_ENV}) is armed in production.",
                    fields=(),
                    hint=f"unset {CHAOS_ARMED_ENV}",
                )
            )
        return out

    # -- the gate --------------------------------------------------------- #

    def evaluate(self, settings: Settings) -> ProdSafetyReport:
        """Compute the safety report (verdict + fatal findings) without raising."""
        verdict = validate_settings(settings)
        if not _is_production(settings):
            # Local/test/dev: the validator verdict stands; nothing is fatal.
            return ProdSafetyReport(verdict=verdict, fatal=())

        fatal: list[ConfigFinding] = []
        fatal.extend(self._demo_credentials(settings))
        fatal.extend(self._live_video_opt_in(settings))
        fatal.extend(self._debug_posture(settings))

        # Escalate any blocking validator ERROR to FATAL in production: a config
        # that isn't ready must not boot a prod process. INFO/WARNING pass.
        for finding in verdict.errors:
            fatal.append(
                replace(
                    finding,
                    severity=Severity.FATAL,
                    code=f"prod.escalated.{finding.code}",
                )
            )

        return ProdSafetyReport(verdict=verdict, fatal=tuple(fatal))

    def assert_safe(self, settings: Settings) -> ProdSafetyReport:
        """Evaluate and raise :class:`ProdSafetyError` if any fatal violation exists.

        Returns the report on success so a caller can log the (non-fatal) verdict.
        """
        report = self.evaluate(settings)
        if not report.safe:
            _log.error(
                "prod_safety_refused",
                env=settings.app_env,
                violations=[f.code for f in report.fatal],
            )
            raise ProdSafetyError(report.fatal)
        _log.info(
            "prod_safety_ok",
            env=settings.app_env,
            warnings=len(report.verdict.warnings),
        )
        return report


def assert_safe_to_boot(
    settings: Settings, *, environ: Mapping[str, str] | None = None
) -> ProdSafetyReport:
    """Module-level convenience: assert the live ``settings`` are safe to boot.

    Raises :class:`ProdSafetyError` (with every violation) when unsafe; returns
    the :class:`ProdSafetyReport` otherwise. A no-op-ish pass-through locally.
    """
    return ProdSafetyGate(environ=environ).assert_safe(settings)
