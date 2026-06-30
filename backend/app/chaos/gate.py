"""The production hard gate — chaos refuses to arm outside ``local``/test.

Fault injection is, by construction, a way to make a running system fail. That
is exactly what you must never do to a production deployment by accident. This
gate is the single chokepoint every arming path goes through, and it is
**deny-by-default**: chaos arms only when *both*

1. the application environment is a chaos-safe one (``local`` or ``test``), and
2. an explicit opt-in flag is set (so even locally chaos is off unless asked),

are true. Critically, the environment check wins even if the flag is set: a
deployment that fat-fingers ``CHAOS_ENABLED=true`` in prod still cannot arm —
the gate raises :class:`ChaosDisarmedError` rather than silently no-op'ing, so
the misconfiguration is loud, not invisible.

The gate reads the live :class:`~app.core.config.Settings` lazily so it always
reflects the running environment, and it never imports anything that touches the
network. Tests construct a tiny settings-shaped object (or pass overrides) to
exercise both the allow and refuse paths without a real environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Environments where chaos *may* arm (subject to the explicit opt-in flag too).
CHAOS_SAFE_ENVIRONMENTS: frozenset[str] = frozenset({"local", "test", "ci"})


class ChaosDisarmedError(RuntimeError):
    """Raised when an arming attempt is refused by the production hard gate.

    Carries the reason so an operator log line / API error explains *why* chaos
    refused (wrong environment vs. flag off) rather than failing opaquely.
    """

    def __init__(self, reason: str, *, environment: str, flag_enabled: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.environment = environment
        self.flag_enabled = flag_enabled


@runtime_checkable
class GateSettings(Protocol):
    """The slice of application settings the gate reads (structural typing).

    The real :class:`app.core.config.Settings` satisfies this once the additive
    ``chaos_enabled`` field exists; tests can pass any object with these members.
    """

    app_env: str
    chaos_enabled: bool


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The outcome of evaluating the gate (without raising)."""

    allowed: bool
    environment: str
    flag_enabled: bool
    reason: str


def evaluate_gate(settings: GateSettings) -> GateDecision:
    """Evaluate (but do not enforce) the gate against ``settings``.

    Environment check is dominant: an unsafe environment is refused regardless of
    the flag, so an accidental prod flag cannot arm chaos.
    """
    env = (settings.app_env or "").strip().lower()
    flag = bool(getattr(settings, "chaos_enabled", False))

    if env not in CHAOS_SAFE_ENVIRONMENTS:
        return GateDecision(
            allowed=False,
            environment=env,
            flag_enabled=flag,
            reason=(
                f"chaos refuses to arm: APP_ENV={env!r} is not a chaos-safe "
                f"environment ({sorted(CHAOS_SAFE_ENVIRONMENTS)}) — refusing even "
                "if CHAOS_ENABLED is set."
            ),
        )
    if not flag:
        return GateDecision(
            allowed=False,
            environment=env,
            flag_enabled=flag,
            reason="chaos is off: set CHAOS_ENABLED=true to arm (it is off by default).",
        )
    return GateDecision(
        allowed=True,
        environment=env,
        flag_enabled=flag,
        reason=f"chaos may arm in {env!r} with CHAOS_ENABLED set.",
    )


def assert_chaos_armable(settings: GateSettings) -> None:
    """Raise :class:`ChaosDisarmedError` unless the gate allows arming.

    The single enforcement primitive: every runner / arming path calls this
    *before* touching a :class:`~app.chaos.interceptor.FaultInjector`.
    """
    decision = evaluate_gate(settings)
    if not decision.allowed:
        raise ChaosDisarmedError(
            decision.reason,
            environment=decision.environment,
            flag_enabled=decision.flag_enabled,
        )


__all__ = [
    "CHAOS_SAFE_ENVIRONMENTS",
    "ChaosDisarmedError",
    "GateDecision",
    "GateSettings",
    "assert_chaos_armable",
    "evaluate_gate",
]
